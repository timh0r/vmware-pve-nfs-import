#!/usr/bin/env python3
"""
pve_nfs_vmware_import.py

One-Script-Tool fuer den Import von VMware-VMs, deren VMDK-Dateien auf
einem NFS-Share liegen, nach Proxmox VE.

Ablauf:
- VMware .vmx Dateien auf NFS suchen
- .vmx analysieren
- Proxmox VM-Konfiguration vorbereiten
- lokale VMDK-Descriptoren unter /var/lib/vz/images/<VMID>/ erzeugen
- Descriptoren auf die NFS *-flat.vmdk Dateien zeigen lassen
- VM in Proxmox starten
- Disks danach einzeln auf Ziel-Storage migrieren

Dry-Run:
- Mit --dry-run werden keine veraendernden Proxmox-Kommandos ausgefuehrt
- Descriptor-Dateien werden nicht geschrieben
- VM-Start und Disk-Migration werden nur geloggt

Autor: Datimo / intern

Installation:

Install dependencies:
apt install python3.13-venv

mkdir -p /opt/pve-nfs-import
cd /opt/pve-nfs-import

python3 -m venv .venv
source .venv/bin/activate

pip install rich questionary pydantic

Usage:

cd /opt/pve-nfs-import
source .venv/bin/activate
python pve_nfs_vmware_import.py

"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import questionary
from rich.console import Console
from rich.panel import Panel
from rich.table import Table


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

console = Console(emoji=False)

DEFAULT_MACHINE = "q35"
DEFAULT_CPU = "x86-64-v2-AES"
DEFAULT_SCSIHW = "virtio-scsi-single"
DEFAULT_NIC_MODEL = "virtio"
DEFAULT_STATE_DIR = Path("/opt/pve-nfs-import/state")
DEFAULT_LOG_DIR = Path("/opt/pve-nfs-import/logs")
DEFAULT_DESCRIPTOR_BASE_DIR = Path("/var/lib/vz/images")
DEFAULT_NFS_ROOT = Path("/mnt/vmware-nfs")

LOG = logging.getLogger("pve-nfs-vmware-import")


@dataclass
class RuntimeConfig:
    dry_run: bool = False
    offline_test: bool = False
    state_dir: Path = DEFAULT_STATE_DIR
    log_dir: Path = DEFAULT_LOG_DIR
    descriptor_base_dir: Path = DEFAULT_DESCRIPTOR_BASE_DIR
    nfs_root: Optional[Path] = None
    auto_yes: bool = False


RUNTIME = RuntimeConfig()


# -----------------------------------------------------------------------------
# Data Classes
# -----------------------------------------------------------------------------

@dataclass
class VMwareDisk:
    vmware_key: str
    file_name: str
    descriptor_path: Path
    flat_path: Path
    proxmox_bus: str
    size_bytes: Optional[int] = None


@dataclass
class VMwareNic:
    index: int
    vmware_network: str
    mac: Optional[str] = None
    proxmox_bridge: Optional[str] = None
    virtual_dev: Optional[str] = None
    dvs_portgroup_id: Optional[str] = None
    dvs_port_id: Optional[str] = None
    pci_slot: Optional[str] = None


@dataclass
class VMwareVM:
    name: str
    vmx_path: Path
    memory_mib: int
    cores: int
    guest_os: Optional[str]
    firmware: Optional[str]
    uuid_bios: Optional[str]
    disks: list[VMwareDisk] = field(default_factory=list)
    nics: list[VMwareNic] = field(default_factory=list)


@dataclass
class ImportPlan:
    vm: VMwareVM
    vmid: int
    target_storage: str
    machine: str = DEFAULT_MACHINE
    cpu: str = DEFAULT_CPU
    scsihw: str = DEFAULT_SCSIHW
    nic_model: str = DEFAULT_NIC_MODEL
    create_efi: bool = False
    create_tpm: bool = False
    enable_guest_agent: bool = True
    boot_disk_bus: Optional[str] = None


# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------

def setup_logging(log_dir: Path, dry_run: bool) -> Path:
    """Initialisiert sauberes Datei- und Konsolen-Logging."""
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        fallback = Path.cwd() / "logs"
        fallback.mkdir(parents=True, exist_ok=True)
        log_dir = fallback

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = "dryrun" if dry_run else "run"
    log_file = log_dir / f"pve-nfs-import-{timestamp}-{suffix}.log"

    LOG.setLevel(logging.DEBUG)
    LOG.handlers.clear()

    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-8s %(message)s"
    ))

    console_handler = logging.StreamHandler(stream=sys.stderr)
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))

    LOG.addHandler(file_handler)
    LOG.addHandler(console_handler)

    LOG.info("Logging initialized: %s", log_file)
    LOG.info("Dry-run mode: %s", dry_run)
    return log_file


def log_command(cmd: list[str], dry_run_skipped: bool = False) -> None:
    rendered = " ".join(shlex.quote(part) for part in cmd)
    if dry_run_skipped:
        LOG.info("DRY-RUN skip command: %s", rendered)
        console.print(f"[cyan]DRY-RUN:[/cyan] {rendered}")
    else:
        LOG.debug("Execute command: %s", rendered)


# -----------------------------------------------------------------------------
# Shell Helper
# -----------------------------------------------------------------------------

def offline_command_stdout(cmd: list[str]) -> str:
    """
    Liefert Dummy-Ausgaben fuer Proxmox-Kommandos im Offline-Testmodus.

    Dadurch kann das Script auf einem normalen Rechner getestet werden, ohne dass
    qm oder pvesm installiert sind.
    """
    if cmd[:2] == ["qm", "list"]:
        return (
            "      VMID NAME                 STATUS     MEM(MB)    BOOTDISK(GB) PID\n"
            "       100 dummy-a              stopped    1024       10.00        0\n"
            "       101 dummy-b              stopped    2048       20.00        0\n"
            "       200 dummy-c              stopped    4096       40.00        0\n"
        )

    if cmd[:2] == ["pvesm", "status"]:
        return (
            "Name             Type     Status           Total            Used       Available        %\n"
            "local-lvm        lvmthin  active       100000000        10000000        90000000   10.00%\n"
            "ceph-vm          rbd      active      1000000000       200000000       800000000   20.00%\n"
            "nvme-zfs         zfspool  active       500000000       100000000       400000000   20.00%\n"
        )

    if cmd[:2] == ["qm", "status"] and len(cmd) >= 3:
        return "status: offline-test\n"

    if cmd[:3] == ["qm", "disk", "move"] and "--help" in cmd:
        return "qm disk move <vmid> <disk> <storage> [OPTIONS]\n"

    return ""

def run(
    cmd: list[str],
    check: bool = True,
    readonly: bool = False,
) -> subprocess.CompletedProcess:
    """
    Fuehrt ein Kommando sicher ohne shell=True aus.

    readonly=True:
        Das Kommando wird auch im Dry-Run ausgefuehrt, z.B. qm list,
        pvesm status oder qm status.

    readonly=False:
        Im Dry-Run wird das Kommando nicht ausgefuehrt, sondern nur geloggt.
    """
    if RUNTIME.offline_test and cmd and cmd[0] in {"qm", "pvesm"}:
        log_command(cmd, dry_run_skipped=True)
        return subprocess.CompletedProcess(cmd, 0, stdout=offline_command_stdout(cmd), stderr="")

    if RUNTIME.dry_run and not readonly:
        log_command(cmd, dry_run_skipped=True)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    log_command(cmd)
    try:
        result = subprocess.run(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        LOG.error("Command not found: %s", cmd[0] if cmd else cmd)
        if check:
            raise
        return subprocess.CompletedProcess(cmd, 127, stdout="", stderr=str(exc))

    LOG.debug("Command return code: %s", result.returncode)
    if result.stdout:
        LOG.debug("STDOUT:\n%s", result.stdout.rstrip())
    if result.stderr:
        LOG.debug("STDERR:\n%s", result.stderr.rstrip())

    if check and result.returncode != 0:
        rendered = " ".join(shlex.quote(part) for part in cmd)
        console.print(f"[red]Command failed:[/red] {rendered}")
        console.print(f"[red]STDOUT:[/red]\n{result.stdout}")
        console.print(f"[red]STDERR:[/red]\n{result.stderr}")
        LOG.error("Command failed: %s", rendered)
        raise RuntimeError("Command failed")

    return result

def render_move_disk_command(vmid: int, disk: str, target_storage: str) -> list[str]:
    """
    Baut den bevorzugten qm-disk-move-Befehl.

    Fuer den tmux-Job verwenden wir die moderne Syntax.
    Falls du alte Proxmox-Versionen unterstuetzen willst, kann das Script
    im Shell-Job noch auf qm move_disk fallbacken.
    """
    return [
        "qm", "disk", "move",
        str(vmid),
        disk,
        target_storage,
        "--delete", "1",
    ]


def build_disk_migration_script(plans: list[ImportPlan]) -> Path:
    """
    Erstellt ein Shell-Script, das alle Disk-Migrationen sequenziell ausfuehrt.
    """
    RUNTIME.log_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    script_path = RUNTIME.log_dir / f"disk-migration-{timestamp}.sh"
    job_log = RUNTIME.log_dir / f"disk-migration-{timestamp}.log"

    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        f'LOG_FILE="{job_log}"',
        'exec > >(tee -a "$LOG_FILE") 2>&1',
        "",
        'echo "Disk migration started: $(date -Is)"',
        "",
    ]

    for plan in plans:
        for disk in plan.vm.disks:
            modern_cmd = render_move_disk_command(
                plan.vmid,
                disk.proxmox_bus,
                plan.target_storage,
            )

            fallback_cmd = [
                "qm", "move_disk",
                str(plan.vmid),
                disk.proxmox_bus,
                plan.target_storage,
                "--delete", "1",
            ]

            lines.extend([
                f'echo ""',
                f'echo "Migrating VM {plan.vm.name} ({plan.vmid}) disk {disk.proxmox_bus} to {plan.target_storage}: $(date -Is)"',
                f'if {" ".join(shlex.quote(part) for part in modern_cmd)}; then',
                f'  echo "OK: VM {plan.vmid} {disk.proxmox_bus}"',
                "else",
                f'  echo "Modern qm disk move failed, trying fallback qm move_disk for VM {plan.vmid} {disk.proxmox_bus}"',
                f'  {" ".join(shlex.quote(part) for part in fallback_cmd)}',
                "fi",
                f'echo "Finished VM {plan.vmid} disk {disk.proxmox_bus}: $(date -Is)"',
                "",
            ])

    lines.extend([
        'echo ""',
        'echo "Disk migration finished: $(date -Is)"',
        "",
    ])

    script_path.write_text("\n".join(lines))
    script_path.chmod(0o750)

    LOG.info("Disk migration script written: %s", script_path)
    LOG.info("Disk migration log: %s", job_log)

    return script_path


def start_disk_migration_detached(plans: list[ImportPlan]) -> None:
    """
    Startet die Disk-Migration detached.

    Prioritaet:
    - tmux
    - screen
    - systemd-run
    """
    if RUNTIME.dry_run:
        runner = get_detach_runner() or "no detached runner found"
        console.print(
            f"[cyan]DRY-RUN:[/cyan] Would start disk migration in the background "
            f"using: {runner}"
        )
        return

    runner = get_detach_runner()
    if not runner:
        console.print(
            "[red]No tool for background jobs found.[/red]\n"
            "Please install one of the following:\n"
            "  apt install tmux\n"
            "  apt install screen\n\n"
            "Alternative without an interactive session:\n"
            "  systemd-run is usually included with systemd."
        )
        return

    script_path = build_disk_migration_script(plans)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    job_name = f"pve-migrate-{timestamp}"

    if runner == "tmux":
        run([
            "tmux",
            "new-session",
            "-d",
            "-s",
            job_name,
            str(script_path),
        ])

        console.print(Panel.fit(
            "[bold green]Disk migration started in the background[/bold green]\n\n"
            f"Runner: tmux\n"
            f"Session: {job_name}\n"
            f"Script: {script_path}\n\n"
            f"View session:\n"
            f"  tmux attach -t {job_name}\n\n"
            f"Detach from tmux:\n"
            f"  CTRL-b, then d"
        ))
        return

    if runner == "screen":
        run([
            "screen",
            "-dmS",
            job_name,
            str(script_path),
        ])

        console.print(Panel.fit(
            "[bold green]Disk migration started in the background[/bold green]\n\n"
            f"Runner: screen\n"
            f"Session: {job_name}\n"
            f"Script: {script_path}\n\n"
            f"View session:\n"
            f"  screen -r {job_name}\n\n"
            f"Detach from screen:\n"
            f"  CTRL-a, then d"
        ))
        return

    if runner == "systemd-run":
        run([
            "systemd-run",
            "--unit", job_name,
            "--description", "Proxmox VM disk migration",
            "--collect",
            str(script_path),
        ])

        console.print(Panel.fit(
            "[bold green]Disk migration started as a transient systemd unit[/bold green]\n\n"
            f"Runner: systemd-run\n"
            f"Unit: {job_name}.service\n"
            f"Script: {script_path}\n\n"
            f"Status:\n"
            f"  systemctl status {job_name}.service\n\n"
            f"Logs:\n"
            f"  journalctl -u {job_name}.service -f"
        ))
        return

def get_detach_runner() -> Optional[str]:
    """
    Ermittelt das beste verfuegbare Tool fuer detached Jobs.

    Prioritaet:
    1. tmux      - interaktiv wieder anhaengbar
    2. screen    - interaktiv wieder anhaengbar
    3. systemd-run - sauberer systemd Job, aber nicht interaktiv
    """
    for command in ["tmux", "screen", "systemd-run"]:
        if command_exists(command):
            return command

    return None



# -----------------------------------------------------------------------------
# VMX / VMDK Parsing
# -----------------------------------------------------------------------------

EXTENT_RE = re.compile(r'^(RW\s+\d+\s+\S+\s+)"([^"]+)"(.*)$')
DISK_KEY_RE = re.compile(r"^(scsi|sata|ide)(\d+):(\d+)\.fileName$")
NIC_NETWORK_RE = re.compile(r"^ethernet(\d+)\.networkName$")
NIC_ANY_RE = re.compile(r"^ethernet(\d+)\.")
NIC_MAC_KEYS = ["address", "generatedAddress"]
SNAPSHOT_DISK_RE = re.compile(r"-\d{6}\.vmdk$", re.IGNORECASE)


def parse_vmx(path: Path) -> dict[str, str]:
    """Liest eine VMware .vmx Datei als einfache Key/Value-Struktur."""
    LOG.info("Parse VMX: %s", path)
    data: dict[str, str] = {}

    for raw_line in path.read_text(errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)

        key = key.strip()
        value = value.strip().strip('"')

        data[key] = value
        data[key.lower()] = value

    return data

def parse_int_value(value: Optional[str], default: int, field_name: str) -> int:
    """
    Parst Integer-Werte aus der VMX robust.

    VMware-Werte stehen oft als Strings in Quotes.
    Falls ein Wert fehlt oder ungueltig ist, wird der Default verwendet und geloggt.
    """
    if value is None:
        LOG.warning("%s is missing from VMX; using default: %s", field_name, default)
        return default

    try:
        return int(str(value).strip())
    except ValueError:
        LOG.warning(
            "%s could not be read as an integer: %r; using default: %s",
            field_name,
            value,
            default,
        )
        return default

def find_flat_from_descriptor(descriptor_path: Path) -> Path:
    """Liest aus einem VMDK-Descriptor die referenzierte Flat-Datei."""
    for line in descriptor_path.read_text(errors="replace").splitlines():
        match = EXTENT_RE.match(line.strip())
        if not match:
            continue

        _, flat_file, _ = match.groups()
        flat_path = Path(flat_file)
        if not flat_path.is_absolute():
            flat_path = descriptor_path.parent / flat_path

        return flat_path.resolve()

    raise ValueError(f"No flat VMDK found in descriptor: {descriptor_path}")


EXTENT_RE = re.compile(r'^(RW\s+\d+\s+\S+\s+)"([^"]+)"(.*)$')


def write_local_descriptor(
    source_descriptor: Path,
    local_descriptor: Path,
    flat_path: Path,
    dry_run: bool = False,
) -> None:
    """
    Kopiert den originalen VMware-VMDK-Descriptor lokal nach Proxmox und ersetzt
    nur die Extent-Zeile.

    Beispiel vorher:
        RW 134217728 VMFS "RTS-Mig-Test_4-flat.vmdk"

    Beispiel nachher:
        RW 134217728 VMFS "/mnt/PFAD/ZU/NFS/VM/RTS-Mig-Test_4-flat.vmdk"

    Dadurch bleiben CID, parentCID, createType, ddb.* und weitere VMware-Metadaten erhalten.
    """
    lines = source_descriptor.read_text(errors="replace").splitlines()
    rewritten: list[str] = []
    changed = False

    for line in lines:
        stripped = line.strip()
        match = EXTENT_RE.match(stripped)

        if match:
            prefix, _old_flat, suffix = match.groups()
            rewritten.append(f'{prefix}"{flat_path}"{suffix}')
            changed = True
        else:
            rewritten.append(line)

    if not changed:
        raise ValueError(
            f"No extent line found in VMDK descriptor: {source_descriptor}"
        )

    if dry_run:
        console.print(
            f"[cyan]DRY-RUN:[/cyan] Would write VMDK descriptor: "
            f"{local_descriptor} -> Flat: {flat_path}"
        )
        return

    local_descriptor.parent.mkdir(parents=True, exist_ok=True)
    local_descriptor.write_text("\n".join(rewritten) + "\n")


def normalize_vmware_uuid(value: Optional[str]) -> Optional[str]:
    if not value:
        return None

    cleaned = value.replace(" ", "").replace("-", "").lower()
    if len(cleaned) != 32:
        LOG.warning("Could not normalize VMware UUID: %s", value)
        return None

    return (
        f"{cleaned[0:8]}-"
        f"{cleaned[8:12]}-"
        f"{cleaned[12:16]}-"
        f"{cleaned[16:20]}-"
        f"{cleaned[20:32]}"
    )


def discover_vms(nfs_root: Path) -> list[Path]:
    """Sucht rekursiv nach .vmx Dateien auf dem NFS-Share."""
    LOG.info("Searching for VMX files under: %s", nfs_root)
    return sorted(nfs_root.rglob("*.vmx"))

def disk_sort_key(file_name: str) -> tuple[int, int, str]:
    """
    Sortiert VMware-VMDKs so, dass die Basisdisk ohne Nummer zuerst kommt.

    Beispiele:
      RTS-Mig-Test.vmdk      -> zuerst
      RTS-Mig-Test_1.vmdk
      RTS-Mig-Test_2.vmdk
      RTS-Mig-Test_10.vmdk   -> nach _2
    """
    name = Path(file_name).name
    stem = Path(file_name).stem

    match = re.search(r"_(\d+)$", stem)
    if match:
        return (1, int(match.group(1)), name.lower())

    return (0, 0, name.lower())

def assign_default_scsi_ports(disks: list[VMwareDisk]) -> None:
    """
    Weist nach der sortierten Reihenfolge Default-SCSI-Ports zu.
    Die niedrigste bzw. Basis-VMDK landet dadurch auf scsi0.
    """
    for index, disk in enumerate(sorted(disks, key=lambda d: disk_sort_key(d.file_name))):
        disk.proxmox_bus = f"scsi{index}"

def get_file_size(path: Path) -> Optional[int]:
    try:
        return path.stat().st_size
    except OSError as exc:
        LOG.warning("Could not read file size: %s: %s", path, exc)
        return None

def format_size(size_bytes: Optional[int]) -> str:
    """
    Formatiert Bytes als gut lesbare GB/TB-Anzeige.

    Verwendet binaere Einheiten:
    - GB = GiB
    - TB = TiB

    Die Anzeige bleibt bewusst als GB/TB beschriftet, weil das fuer
    Migrations-Reviews praxisnaeher lesbar ist.
    """
    if size_bytes is None:
        return "-"

    gib = size_bytes / (1024 ** 3)

    if gib >= 1024:
        tib = gib / 1024
        return f"{tib:.2f} TB"

    return f"{gib:.1f} GB"

def extract_disks(data: dict[str, str], vmx_path: Path) -> list[VMwareDisk]:
    disks: list[VMwareDisk] = []

    for key, file_name in sorted(data.items()):
        match = DISK_KEY_RE.match(key)
        if not match:
            continue

        if not file_name.lower().endswith(".vmdk"):
            continue

        descriptor_path = (vmx_path.parent / file_name).resolve()

        if SNAPSHOT_DISK_RE.search(file_name):
            console.print(
                f"[yellow]Warning: Snapshot VMDK detected:[/yellow] {descriptor_path}"
            )
            LOG.warning("Snapshot VMDK detected: %s", descriptor_path)

        if not descriptor_path.exists():
            console.print(f"[yellow]Warning: Descriptor not found:[/yellow] {descriptor_path}")
            LOG.warning("Descriptor not found: %s", descriptor_path)
            continue

        flat_path = find_flat_from_descriptor(descriptor_path)
        if not flat_path.exists():
            console.print(f"[yellow]Warning: Flat VMDK not found:[/yellow] {flat_path}")
            LOG.warning("Flat VMDK not found: %s", flat_path)
            continue

        disks.append(VMwareDisk(
            vmware_key=key,
            file_name=file_name,
            descriptor_path=descriptor_path,
            flat_path=flat_path,
            proxmox_bus="",
            size_bytes=get_file_size(flat_path),
        ))

    assign_default_scsi_ports(disks)
    return sorted(disks, key=lambda d: int(d.proxmox_bus.replace("scsi", "")))

def ask_scsi_port_for_disk(
    vm_name: str,
    disk: VMwareDisk,
    used_ports: set[str],
) -> str:
    """
    Fragt den gewünschten Proxmox-SCSI-Port fuer eine VMware-Disk ab.
    """
    choices = []
    for index in range(0, 32):
        port = f"scsi{index}"
        if port in used_ports and port != disk.proxmox_bus:
            continue

        title = port
        if port == disk.proxmox_bus:
            title += " (Default)"

        choices.append(questionary.Choice(title=title, value=port))

    value = questionary.select(
        (
            f"{vm_name}: SCSI port for {disk.descriptor_path.name} "
            f"[{format_size(disk.size_bytes)}] "
            f"({disk.vmware_key}, flat={disk.flat_path.name}):"
        ),
        choices=choices,
        default=disk.proxmox_bus or "scsi0",
    ).ask()

    if value is None:
        raise KeyboardInterrupt

    return str(value)

def ask_disk_scsi_mapping(vm: VMwareVM) -> None:
    """
    Laesst den Benutzer alle importierten VMDKs auf Proxmox-SCSI-Ports mappen.
    """
    if not vm.disks:
        return

    console.print(Panel.fit(
        f"[bold]Disk/SCSI-Mapping:[/bold] {vm.name}\n\n"
        "The base VMDK without a number is assigned to scsi0 by default.\n"
        "You can change the ports here if needed."
    ))

    used_ports: set[str] = set()

    for disk in sorted(vm.disks, key=lambda d: int(d.proxmox_bus.replace("scsi", ""))):
        selected_port = ask_scsi_port_for_disk(vm.name, disk, used_ports)
        disk.proxmox_bus = selected_port
        used_ports.add(selected_port)

    vm.disks.sort(key=lambda d: int(d.proxmox_bus.replace("scsi", "")))

def extract_nics(data: dict[str, str]) -> list[VMwareNic]:
    """
    Erkennt VMware-NICs robuster als nur ueber ethernetX.networkName.

    Manche VMX-Dateien enthalten zwar ethernetX.present, ethernetX.virtualDev
    oder MAC-Felder, aber kein networkName. In diesem Fall wird die NIC trotzdem
    im Wizard angezeigt, damit das Bridge-Mapping nicht uebersprungen wird.
    """
    indexes: set[int] = set()

    for key in data:
        match = NIC_ANY_RE.match(key)
        if match:
            indexes.add(int(match.group(1)))

    nics: list[VMwareNic] = []
    for index in sorted(indexes):
        present = data.get(f"ethernet{index}.present", "TRUE").strip().lower()
        if present in {"false", "0", "no"}:
            LOG.info("NIC ethernet%s is marked as not present in the VMX", index)
            continue

        virtual_dev = data.get(f"ethernet{index}.virtualDev")
        dvs_portgroup_id = data.get(f"ethernet{index}.dvs.portgroupId")
        dvs_port_id = data.get(f"ethernet{index}.dvs.portId")
        pci_slot = data.get(f"ethernet{index}.pciSlotNumber")

        # dvPortgroup-Namen stehen in VMX-Exports haeufig nicht im Klartext.
        # Deshalb ist die primaere Benutzeranzeige bewusst NIC-Index + MAC.
        network_name = (
            data.get(f"ethernet{index}.networkName")
            or data.get(f"ethernet{index}.opaqueNetwork.id")
            or data.get(f"ethernet{index}.deviceName")
            or dvs_portgroup_id
            or f"ethernet{index}"
        )

        mac = None
        for mac_key in NIC_MAC_KEYS:
            candidate = data.get(f"ethernet{index}.{mac_key}")
            if candidate:
                mac = candidate
                break

        nics.append(VMwareNic(
            index=index,
            vmware_network=network_name,
            mac=mac,
            virtual_dev=virtual_dev,
            dvs_portgroup_id=dvs_portgroup_id,
            dvs_port_id=dvs_port_id,
            pci_slot=pci_slot,
        ))

    LOG.info(
        "NICs detected: %s",
        [
            f"NIC{n.index}: mac={n.mac or '-'} source={n.vmware_network} bridge={n.proxmox_bridge or '-'}"
            for n in nics
        ],
    )
    return nics


def build_vm_from_vmx(vmx_path: Path) -> VMwareVM:
    data = parse_vmx(vmx_path)
    LOG.debug(
        "VMX raw values for %s: memsize=%r memSize=%r numvcpus=%r numvCPUs=%r coresPerSocket=%r",
        vmx_path,
        data.get("memsize"),
        data.get("memSize"),
        data.get("numvcpus"),
        data.get("numvCPUs"),
        data.get("cpuid.corespersocket"),
    )

    name = data.get("displayName") or vmx_path.parent.name
    memory_mib = parse_int_value(
        data.get("memsize") or data.get("memSize"),
        1024,
        "memsize",
    )

    cores = parse_int_value(
        data.get("numvcpus") or data.get("numvCPUs") or data.get("cpuid.corespersocket"),
        1,
        "numvcpus",
    )
    firmware = data.get("firmware")
    guest_os = data.get("guestOS")
    uuid_bios = normalize_vmware_uuid(data.get("uuid.bios"))

    vm = VMwareVM(
        name=name,
        vmx_path=vmx_path,
        memory_mib=memory_mib,
        cores=cores,
        guest_os=guest_os,
        firmware=firmware,
        uuid_bios=uuid_bios,
    )
    vm.disks = extract_disks(data, vmx_path)
    vm.nics = extract_nics(data)

    LOG.info(
        "VM detected: name=%s vmx=%s memory=%s cores=%s disks=%s nics=%s firmware=%s",
        vm.name,
        vm.vmx_path,
        vm.memory_mib,
        vm.cores,
        len(vm.disks),
        len(vm.nics),
        vm.firmware or "bios",
    )
    return vm


# -----------------------------------------------------------------------------
# Proxmox Inventory
# -----------------------------------------------------------------------------

def get_used_vmids() -> set[int]:
    """Liest alle vorhandenen VMIDs aus qm list."""
    if RUNTIME.offline_test:
        LOG.info("OFFLINE TEST: Using dummy VMIDs")
        return {100, 101, 200}

    result = run(["qm", "list"], check=True, readonly=True)
    used: set[int] = set()

    for line in result.stdout.splitlines()[1:]:
        parts = line.split()
        if parts and parts[0].isdigit():
            used.add(int(parts[0]))

    LOG.info("VMIDs in use: %s", sorted(used))
    return used

def suggest_next_vmid(used_vmids: set[int], start: int = 10) -> int:
    try:
        vmid = max(get_next_cluster_vmid(), start)
    except Exception as exc:
        LOG.warning("pvesh /cluster/nextid failed; falling back to used VMIDs: %s", exc)
        highest = max(used_vmids) if used_vmids else start - 1
        vmid = max(highest + 1, start)

    while vmid in used_vmids:
        vmid += 1

    return vmid


def get_next_cluster_vmid() -> int:
    if RUNTIME.offline_test:
        LOG.info("OFFLINE TEST: Using dummy next ID 201")
        return 201

    result = run(["pvesh", "get", "/cluster/nextid"], check=True, readonly=True)
    return int(result.stdout.strip())


def get_used_cluster_vmids() -> set[int]:
    if RUNTIME.offline_test:
        LOG.info("OFFLINE TEST: Using dummy VMIDs")
        return {100, 101, 200}

    result = run(
        ["pvesh", "get", "/cluster/resources", "--type", "vm", "--output-format", "json"],
        check=True,
        readonly=True,
    )

    resources = json.loads(result.stdout)
    return {
        int(item["vmid"])
        for item in resources
        if "vmid" in item
    }


def ask_vmid(default_vmid: int, used_vmids: set[int], vm_name: str) -> int:
    """Fragt eine VMID ab und verhindert Doppelbelegungen im aktuellen Plan."""
    while True:
        raw = questionary.text(
            f"VMID for {vm_name}:",
            default=str(default_vmid),
        ).ask()

        if raw is None:
            raise KeyboardInterrupt

        try:
            vmid = int(raw)
        except ValueError:
            console.print("[red]Please enter a valid numeric VMID.[/red]")
            continue

        if vmid in used_vmids:
            console.print(f"[red]VMID {vmid} is already in use or reserved in the current plan.[/red]")
            continue

        return vmid

def get_pve_storages() -> list[str]:
    """Gibt Proxmox Storages zurueck."""
    if RUNTIME.offline_test:
        LOG.info("OFFLINE TEST: Using dummy storage")
        return ["local-lvm", "ceph-vm", "nvme-zfs"]
    result = run(["pvesm", "status"], check=True, readonly=True)
    storages: list[str] = []

    for line in result.stdout.splitlines()[1:]:
        parts = line.split()
        if parts:
            storages.append(parts[0])

    LOG.info("Storage detected: %s", storages)
    return storages


def get_pve_bridges() -> list[str]:
    """
    Liefert lokale Linux-Bridges plus Proxmox-SDN-VNets.

    SDN-VNets sind in Proxmox fuer VMs wie Bridges verwendbar. Deshalb werden
    sie zusaetzlich ueber die Cluster-API gelesen.
    """
    if RUNTIME.offline_test:
        LOG.info("OFFLINE TEST: Using dummy bridges, including SDN VNets")
        return ["vmbr0", "vmbr10", "vmbr20", "vnet-prod", "vnet-dmz"]

    bridges: set[str] = set()
    net_dir = Path("/sys/class/net")

    if net_dir.exists():
        for item in net_dir.iterdir():
            if item.name.startswith("vmbr"):
                bridges.add(item.name)

    for vnet in get_pve_sdn_vnets():
        bridges.add(vnet)

    result = sorted(bridges)
    LOG.info("Bridges/VNets detected: %s", result)
    return result

def get_pve_sdn_vnets() -> list[str]:
    """Liest Proxmox-SDN-VNets clusterweit ueber pvesh."""
    if RUNTIME.offline_test:
        return ["vnet-prod", "vnet-dmz"]

    if not command_exists("pvesh"):
        LOG.warning("pvesh not found; cannot query SDN VNets")
        return []

    result = run(
        ["pvesh", "get", "/cluster/sdn/vnets", "--output-format", "json"],
        check=False,
        readonly=True,
    )

    if result.returncode != 0 or not result.stdout.strip():
        LOG.warning("SDN VNet query failed: %s", result.stderr.strip())
        return []

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        LOG.warning("Could not read SDN VNet JSON: %s", exc)
        return []

    vnets: list[str] = []
    for item in data:
        name = item.get("vnet") or item.get("vnetid") or item.get("id")
        if name:
            vnets.append(str(name))

    LOG.info("SDN VNets detected: %s", sorted(vnets))
    return sorted(set(vnets))


def command_exists(command: str) -> bool:
    return shutil.which(command) is not None


# -----------------------------------------------------------------------------
# Wizard / UI
# -----------------------------------------------------------------------------

def wizard_for_vm(
    vm: VMwareVM,
    storages: list[str],
    bridges: list[str],
    used_vmids: set[int],
) -> ImportPlan:
    """
    Fuehrt den VM-Wizard aus.

    Questionary kann innerhalb einzelner Prompts nicht sauber "zurueck" springen.
    Deshalb wird pro VM eine Wizard-Schleife verwendet:
    - Werte erfassen
    - Zusammenfassung anzeigen
    - Benutzer waehlt: Uebernehmen, Zurueck / erneut bearbeiten, Abbrechen

    Wird "Zurueck" gewaehlt, wird die reservierte VMID wieder freigegeben und
    der Wizard fuer diese VM neu gestartet.
    """
    while True:
        plan = collect_vm_wizard_values(vm, storages, bridges, used_vmids)
        show_single_plan_review(plan)

        action = questionary.select(
            f"Apply settings for {vm.name}?",
            choices=[
                questionary.Choice(title="Apply and continue", value="accept"),
                questionary.Choice(title="Back / edit this VM again", value="back"),
                questionary.Choice(title="Cancel", value="abort"),
            ],
            default="accept",
        ).ask()

        if action is None:
            raise KeyboardInterrupt

        if action == "accept":
            return plan

        used_vmids.discard(plan.vmid)
        reset_vm_wizard_values(vm)

        if action == "abort":
            console.print("[yellow]Cancelled.[/yellow]")
            sys.exit(0)

        console.print(f"[cyan]Restarting the wizard for {vm.name}.[/cyan]")


def collect_vm_wizard_values(
    vm: VMwareVM,
    storages: list[str],
    bridges: list[str],
    used_vmids: set[int],
) -> ImportPlan:
    console.print(Panel.fit(f"[bold]Prepare VM:[/bold] {vm.name}"))

    if not vm.disks:
        console.print(f"[yellow]Warning: VM {vm.name} has no importable VMDKs.[/yellow]")

    default_vmid = suggest_next_vmid(used_vmids)
    vmid = ask_vmid(default_vmid, used_vmids, vm.name)
    used_vmids.add(vmid)

    target_storage = questionary.select(
        f"Target storage for {vm.name}:",
        choices=storages,
    ).ask()
    if target_storage is None:
        raise KeyboardInterrupt
    
    ask_disk_scsi_mapping(vm)

    if not vm.nics:
        console.print(
            f"[yellow]No NICs detected in the VMX for {vm.name}. "
            "Network mapping will be skipped.[/yellow]"
        )
        LOG.warning("No NICs detected in VMX: %s", vm.vmx_path)

    for nic in vm.nics:
        bridge = questionary.select(
            f"{vm.name}: Map {format_nic_source(nic)} to a Proxmox bridge:",
            choices=bridges,
        ).ask()
        if bridge is None:
            raise KeyboardInterrupt
        nic.proxmox_bridge = bridge

    boot_disk_bus = None
    if vm.disks:
        suggested_boot_disk = suggest_boot_disk(vm)
        choices = [
            questionary.Choice(
                title=(
                    f"{disk.proxmox_bus} | {disk.descriptor_path.name} | "
                    f"{format_size(disk.size_bytes)} | {disk.vmware_key} | "
                    f"flat={disk.flat_path.name}"
                ),
                value=disk.proxmox_bus,
            )
            for disk in vm.disks
        ]
        boot_disk_bus = questionary.select(
            f"Boot disk for {vm.name}:",
            choices=choices,
            default=suggested_boot_disk.proxmox_bus if suggested_boot_disk else None,
        ).ask()
        if boot_disk_bus is None:
            raise KeyboardInterrupt

    enable_guest_agent = bool(questionary.confirm(
        f"Enable QEMU Guest Agent for {vm.name}?",
        default=True,
    ).ask())

    is_efi = (vm.firmware or "").lower() == "efi"
    create_efi = False
    create_tpm = False

    if is_efi:
        console.print("[yellow]UEFI/EFI VM detected.[/yellow]")
        create_efi = bool(questionary.confirm(
            "Create a new Proxmox EFI disk?",
            default=True,
        ).ask())

        create_tpm = bool(questionary.confirm(
            "Create a new TPM 2.0 device?",
            default=True,
        ).ask())

    return ImportPlan(
        vm=vm,
        vmid=vmid,
        target_storage=target_storage,
        create_efi=create_efi,
        create_tpm=create_tpm,
        enable_guest_agent=enable_guest_agent,
        boot_disk_bus=boot_disk_bus,
    )


def reset_vm_wizard_values(vm: VMwareVM) -> None:
    """Setzt Wizard-seitige Werte zurueck, bevor eine VM erneut bearbeitet wird."""
    for nic in vm.nics:
        nic.proxmox_bridge = None


def suggest_boot_disk(vm: VMwareVM) -> Optional[VMwareDisk]:
    """
    Ermittelt eine plausible Bootdisk anhand der VMware-Key-Reihenfolge.

    Prioritaet:
    1. scsi0:0.fileName
    2. sata0:0.fileName
    3. ide0:0.fileName
    4. erste erkannte Disk
    """
    priorities = [
        "scsi0:0.fileName",
        "sata0:0.fileName",
        "ide0:0.fileName",
    ]
    by_key = {disk.vmware_key: disk for disk in vm.disks}

    for key in priorities:
        if key in by_key:
            return by_key[key]

    return vm.disks[0] if vm.disks else None


def show_single_plan_review(plan: ImportPlan) -> None:
    """Zeigt eine kompakte Zusammenfassung fuer die aktuelle VM vor dem Uebernehmen."""
    vm = plan.vm
    lines = [
        f"VM: {vm.name}",
        f"VMID: {plan.vmid}",
        f"RAM: {vm.memory_mib} MiB",
    ]
    if vm.memory_mib == 1024:
        lines.append("WARNING: RAM is 1024 MiB. This may be a VMX parsing fallback.")

    lines.extend([
        f"CPU: {vm.cores}",
        f"Firmware: {vm.firmware or 'bios'}",
        f"Target storage: {plan.target_storage}",
        f"Boot disk: {plan.boot_disk_bus or '-'}",
        f"QEMU Guest Agent: {'yes' if plan.enable_guest_agent else 'no'}",
        f"Create EFI disk: {'yes' if plan.create_efi else 'no'}",
        f"Create TPM: {'yes' if plan.create_tpm else 'no'}",
        "",
        "NIC mapping:",
        format_nic_mapping(plan),
        "",
        "Disks:",
    ])
    if vm.disks:
        for disk in vm.disks:
            lines.append(
                f"- {disk.proxmox_bus}: {format_size(disk.size_bytes)} | "
                f"{disk.vmware_key} | {disk.descriptor_path.name} -> {disk.flat_path}"
            )
    else:
        lines.append("- No disks detected")

    console.print(Panel.fit("\n".join(lines), title="Wizard Summary"))


def format_nic_source(nic: VMwareNic) -> str:
    """User-facing NIC label fuer Wizard und Plan."""
    parts = [f"NIC{nic.index}"]

    if nic.mac:
        parts.append(nic.mac)

    if nic.dvs_portgroup_id:
        parts.append(f"dvpg={nic.dvs_portgroup_id}")

    if nic.dvs_port_id:
        parts.append(f"port={nic.dvs_port_id}")

    if nic.virtual_dev:
        parts.append(nic.virtual_dev)

    return " | ".join(parts)


def format_nic_mapping(plan: ImportPlan) -> str:
    if not plan.vm.nics:
        return "-"

    return "\n".join(
        f"NIC{nic.index} {nic.mac or '-'} -> {nic.proxmox_bridge or 'unassigned'}"
        for nic in plan.vm.nics
    )


def show_plan(plans: list[ImportPlan]) -> None:
    table = Table(title="Import Plan")
    table.add_column("VM")
    table.add_column("VMID")
    table.add_column("RAM")
    table.add_column("CPU")
    table.add_column("Firmware")
    table.add_column("Disks")
    table.add_column("NICs")
    table.add_column("NIC Mapping")
    table.add_column("Boot Disk")
    table.add_column("QEMU Guest Agent")
    table.add_column("Target Storage")

    for plan in plans:
        vm = plan.vm
        table.add_row(
            vm.name,
            str(plan.vmid),
            f"{vm.memory_mib} MiB",
            str(vm.cores),
            vm.firmware or "bios",
            str(len(vm.disks)),
            str(len(vm.nics)),
            format_nic_mapping(plan),
            plan.boot_disk_bus or "-",
            "yes" if plan.enable_guest_agent else "no",
            plan.target_storage,
        )

    console.print(table)


def show_disk_plan(plans: list[ImportPlan]) -> None:
    table = Table(title="Disk Mapping")
    table.add_column("VM")
    table.add_column("VMID")
    table.add_column("Bus")
    table.add_column("Size")
    table.add_column("VMware Descriptor")
    table.add_column("Flat VMDK")
    table.add_column("Local Descriptor")

    for plan in plans:
        for disk in plan.vm.disks:
            local_descriptor = get_local_descriptor_path(plan, disk)
            table.add_row(
                plan.vm.name,
                str(plan.vmid),
                disk.proxmox_bus,
                format_size(disk.size_bytes),
                str(disk.descriptor_path),
                str(disk.flat_path),
                str(local_descriptor),
            )

    console.print(table)


def show_nic_plan(plans: list[ImportPlan]) -> None:
    table = Table(title="NIC Mapping")
    table.add_column("VM")
    table.add_column("VMID")
    table.add_column("NIC")
    table.add_column("MAC")
    table.add_column("VMware Source")
    table.add_column("Proxmox Bridge")
    table.add_column("Proxmox Net")

    for plan in plans:
        for nic in plan.vm.nics:
            table.add_row(
                plan.vm.name,
                str(plan.vmid),
                f"NIC{nic.index}",
                nic.mac or "-",
                format_nic_source(nic),
                nic.proxmox_bridge or "unassigned",
                f"net{nic.index}",
            )

    console.print(table)


def show_vm_status(plans: list[ImportPlan]) -> None:
    table = Table(title="VM Status")
    table.add_column("VM")
    table.add_column("VMID")
    table.add_column("Status")

    for plan in plans:
        if RUNTIME.offline_test:
            status = "offline test: not queried"
        else:
            result = run(["qm", "status", str(plan.vmid)], check=False, readonly=True)
            status = result.stdout.strip() if result.returncode == 0 else "unknown"
        table.add_row(plan.vm.name, str(plan.vmid), status)

    console.print(table)


# -----------------------------------------------------------------------------
# Proxmox Execution
# -----------------------------------------------------------------------------

def get_local_descriptor_path(plan: ImportPlan, disk: VMwareDisk) -> Path:
    safe_vm_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", plan.vm.name)
    vm_image_dir = RUNTIME.descriptor_base_dir / str(plan.vmid)
    # return vm_image_dir / f"{safe_vm_name}-{disk.proxmox_bus}.vmdk"
    return vm_image_dir / disk.descriptor_path.name


def create_pve_vm(plan: ImportPlan) -> None:
    vm = plan.vm
    LOG.info("Creating Proxmox VM: name=%s vmid=%s", vm.name, plan.vmid)

    cmd = [
        "qm", "create", str(plan.vmid),
        "--name", vm.name,
        "--memory", str(vm.memory_mib),
        "--cores", str(vm.cores),
        "--machine", plan.machine,
        "--cpu", plan.cpu,
        "--scsihw", plan.scsihw,
        "--agent", f"enabled={1 if plan.enable_guest_agent else 0}",
    ]
    run(cmd)

    if vm.uuid_bios:
        run(["qm", "set", str(plan.vmid), "--smbios1", f"uuid={vm.uuid_bios}"])

    if plan.create_efi:
        run(["qm", "set", str(plan.vmid), "--bios", "ovmf"])
        run([
            "qm", "set", str(plan.vmid),
            "--efidisk0",
            f"{plan.target_storage}:1,efitype=4m,pre-enrolled-keys=1",
        ])

    if plan.create_tpm:
        run([
            "qm", "set", str(plan.vmid),
            "--tpmstate0",
            f"{plan.target_storage}:1,version=v2.0",
        ])

    attach_disks(plan)
    attach_nics(plan)

    if plan.boot_disk_bus:
        run(["qm", "set", str(plan.vmid), "--boot", f"order={plan.boot_disk_bus}"])


def attach_disks(plan: ImportPlan) -> None:
    vm = plan.vm
    vm_image_dir = RUNTIME.descriptor_base_dir / str(plan.vmid)

    if RUNTIME.dry_run:
        console.print(
            f"[cyan]DRY-RUN:[/cyan] Would create directory: {vm_image_dir}"
        )
    else:
        vm_image_dir.mkdir(parents=True, exist_ok=True)

    for disk in vm.disks:
        local_descriptor = vm_image_dir / disk.descriptor_path.name

        write_local_descriptor(
            source_descriptor=disk.descriptor_path,
            local_descriptor=local_descriptor,
            flat_path=disk.flat_path,
            dry_run=RUNTIME.dry_run,
        )

        run([
            "qm", "set", str(plan.vmid),
            f"--{disk.proxmox_bus}",
            str(local_descriptor),
        ])


def attach_nics(plan: ImportPlan) -> None:
    for nic in plan.vm.nics:
        if not nic.proxmox_bridge:
            continue

        net_key = f"--net{nic.index}"
        if nic.mac:
            net_value = f"{plan.nic_model}={nic.mac},bridge={nic.proxmox_bridge}"
        else:
            net_value = f"{plan.nic_model},bridge={nic.proxmox_bridge}"

        run(["qm", "set", str(plan.vmid), net_key, net_value])


def confirm_vmware_vm_is_down(plan: ImportPlan) -> bool:
    """
    Fragt unmittelbar vor dem Proxmox-Start ab, ob die zugehoerige
    VMware-VM wirklich ausgeschaltet ist.

    Rueckgabe:
        True  -> Proxmox-Start fuer diese VM darf erfolgen
        False -> Proxmox-Start fuer diese VM wird uebersprungen
    """
    if RUNTIME.auto_yes:
        LOG.warning(
            "Auto-Yes enabled: skipping VMware shutdown confirmation for VM %s (%s)",
            plan.vm.name,
            plan.vmid,
        )
        return True

    disk_lines = []
    for disk in plan.vm.disks:
        disk_lines.append(
            f"- {disk.proxmox_bus}: {disk.descriptor_path.name} -> {disk.flat_path}"
        )

    disks_text = "\n".join(disk_lines) if disk_lines else "- No disks detected"

    console.print(Panel.fit(
        "[bold red]Verify VMware shutdown[/bold red]\n\n"
        f"VM: {plan.vm.name}\n"
        f"Proxmox VMID: {plan.vmid}\n"
        f"VMX: {plan.vm.vmx_path}\n\n"
        "Diese VM darf in Proxmox erst gestartet werden, wenn die VMware-VM "
        "in ESXi/vCenter sauber ausgeschaltet ist.\n\n"
        "Please verify:\n"
        "- VMware-VM ist powered off\n"
        "- keine aktiven Snapshots / keine Snapshot-Chain\n"
        "- keine Schreibzugriffe auf die VMDK-Dateien\n"
        "- NFS-Flat-VMDKs werden nicht mehr von VMware verwendet\n\n"
        f"Disks:\n{disks_text}"
    ))

    answer = questionary.text(
        f"Is VMware VM '{plan.vm.name}' powered off? Type exactly 'yes' to start it; otherwise this VM will be skipped:",
    ).ask()

    if answer is None:
        raise KeyboardInterrupt

    if answer.strip().lower() == "yes":
        LOG.info(
            "VMware shutdown confirmed: VM %s (%s)",
            plan.vm.name,
            plan.vmid,
        )
        return True

    console.print(
        f"[yellow]Skipped starting {plan.vm.name} ({plan.vmid}): "
        "VMware shutdown was not confirmed.[/yellow]"
    )
    LOG.warning(
        "Skipped start because VMware shutdown was not confirmed: VM %s (%s)",
        plan.vm.name,
        plan.vmid,
    )
    return False


def start_vms(plans: list[ImportPlan]) -> list[ImportPlan]:
    """Startet VMs in Proxmox. Vor jedem Start wird einzeln bestaetigt,
    dass die entsprechende VMware-VM ausgeschaltet ist.

    Gibt die Liste der tatsaechlich zum Start freigegebenen VMs zurueck.
    Im Dry-Run wird der qm-start nur geloggt, die Abfrage findet trotzdem statt,
    damit der Ablauf realistisch getestet werden kann.
    """
    started_or_approved: list[ImportPlan] = []

    for plan in plans:
        if not confirm_vmware_vm_is_down(plan):
            continue

        console.print(f"[bold]Starting VM {plan.vm.name} ({plan.vmid})...[/bold]")
        run(["qm", "start", str(plan.vmid)], check=False)
        started_or_approved.append(plan)

    return started_or_approved


def move_disk(vmid: int, disk: str, target_storage: str) -> None:
    """Migriert genau eine Disk. Fallback fuer aeltere qm-Syntax."""
    test = run(["qm", "disk", "move", "--help"], check=False, readonly=True)
    if test.returncode == 0:
        run([
            "qm", "disk", "move",
            str(vmid),
            disk,
            target_storage,
            "--delete", "1",
        ])
        return

    run([
        "qm", "move_disk",
        str(vmid),
        disk,
        target_storage,
        "--delete", "1",
    ])


def move_disks_one_by_one(plans: list[ImportPlan]) -> None:
    for plan in plans:
        for disk in plan.vm.disks:
            console.print(
                f"[bold]Migrating {plan.vm.name} {disk.proxmox_bus} "
                f"to {plan.target_storage}...[/bold]"
            )
            move_disk(plan.vmid, disk.proxmox_bus, plan.target_storage)


# -----------------------------------------------------------------------------
# State / Report
# -----------------------------------------------------------------------------

def plan_to_dict(plan: ImportPlan) -> dict:
    return {
        "vmid": plan.vmid,
        "name": plan.vm.name,
        "vmx_path": str(plan.vm.vmx_path),
        "memory_mib": plan.vm.memory_mib,
        "cores": plan.vm.cores,
        "guest_os": plan.vm.guest_os,
        "firmware": plan.vm.firmware,
        "uuid_bios": plan.vm.uuid_bios,
        "target_storage": plan.target_storage,
        "machine": plan.machine,
        "cpu": plan.cpu,
        "scsihw": plan.scsihw,
        "nic_model": plan.nic_model,
        "create_efi": plan.create_efi,
        "create_tpm": plan.create_tpm,
        "enable_guest_agent": plan.enable_guest_agent,
        "boot_disk_bus": plan.boot_disk_bus,
        "disks": [
            {
                "vmware_key": disk.vmware_key,
                "bus": disk.proxmox_bus,
                "descriptor": str(disk.descriptor_path),
                "flat": str(disk.flat_path),
                "size_bytes": disk.size_bytes,
                "size_human": format_size(disk.size_bytes),
                "local_descriptor": str(get_local_descriptor_path(plan, disk)),
            }
            for disk in plan.vm.disks
        ],
        "nics": [
            {
                "index": nic.index,
                "vmware_network": nic.vmware_network,
                "proxmox_bridge": nic.proxmox_bridge,
                "mac": nic.mac,
                "virtual_dev": nic.virtual_dev,
                "dvs_portgroup_id": nic.dvs_portgroup_id,
                "dvs_port_id": nic.dvs_port_id,
                "pci_slot": nic.pci_slot,
            }
            for nic in plan.vm.nics
        ],
    }


def save_state(plans: list[ImportPlan]) -> Path:
    try:
        RUNTIME.state_dir.mkdir(parents=True, exist_ok=True)
        state_dir = RUNTIME.state_dir
    except PermissionError:
        state_dir = Path.cwd() / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        LOG.warning("State directory is not writable; using fallback: %s", state_dir)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = "dryrun" if RUNTIME.dry_run else "run"
    state_file = state_dir / f"import-{timestamp}-{suffix}.json"

    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "dry_run": RUNTIME.dry_run,
        "descriptor_base_dir": str(RUNTIME.descriptor_base_dir),
        "plans": [plan_to_dict(plan) for plan in plans],
    }

    state_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    LOG.info("State written: %s", state_file)
    return state_file


# -----------------------------------------------------------------------------
# Preflight
# -----------------------------------------------------------------------------

def require_root() -> None:
    if RUNTIME.offline_test:
        LOG.info("OFFLINE TEST: Skipping root check")
        return

    if RUNTIME.dry_run:
        LOG.info("Dry run: root check is not enforced")
        return

    if os.geteuid() != 0:
        console.print("[red]This script must run as root on the Proxmox host.[/red]")
        sys.exit(1)


def require_commands(commands: list[str]) -> None:
    if RUNTIME.offline_test:
        LOG.info("OFFLINE TEST: Skipping command check: %s", commands)
        return
    
    for command in commands:
        if not command_exists(command):
            console.print(f"[red]Required command not found:[/red] {command}")
            sys.exit(1)


def validate_nfs_root(nfs_root: Path) -> None:
    if not nfs_root.exists():
        console.print(f"[red]NFS path does not exist:[/red] {nfs_root}")
        sys.exit(1)

    if not nfs_root.is_dir():
        console.print(f"[red]NFS path is not a directory:[/red] {nfs_root}")
        sys.exit(1)


def confirm_or_abort(message: str, default: bool = False) -> None:
    if RUNTIME.auto_yes:
        LOG.info("Auto-Yes enabled: %s", message)
        return

    confirmed = questionary.confirm(message, default=default).ask()
    if not confirmed:
        console.print("[yellow]Cancelled.[/yellow]")
        sys.exit(0)


# -----------------------------------------------------------------------------
# CLI / Main
# -----------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="VMware NFS Importer for Proxmox VE"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not make changes; only plan and log actions.",
    )
    parser.add_argument(
        "--nfs-root",
        type=Path,
        default=None,
        help="Path to the mounted VMware NFS share.",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=DEFAULT_STATE_DIR,
        help="Directory for state files.",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=DEFAULT_LOG_DIR,
        help="Directory for log files.",
    )
    parser.add_argument(
        "--descriptor-base-dir",
        type=Path,
        default=DEFAULT_DESCRIPTOR_BASE_DIR,
        help="Base directory for local VMDK descriptors.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Automatically confirm prompts where possible.",
    )
    parser.add_argument(
    "--offline-test",
    action="store_true",
    help="Offline test mode without Proxmox commands, root check, or host changes.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    RUNTIME.dry_run = bool(args.dry_run)
    RUNTIME.offline_test = bool(args.offline_test)
    RUNTIME.state_dir = args.state_dir
    RUNTIME.log_dir = args.log_dir
    RUNTIME.descriptor_base_dir = args.descriptor_base_dir
    RUNTIME.nfs_root = args.nfs_root
    RUNTIME.auto_yes = bool(args.yes)

    log_file = setup_logging(RUNTIME.log_dir, RUNTIME.dry_run)

    require_root()
    require_commands(["qm", "pvesm"])
    runner = get_detach_runner()
    if runner:
        LOG.info("Background migration runner detected: %s", runner)
    else:
        LOG.warning("No background migration runner found")

    title = "VMware NFS Importer for Proxmox VE"
    if RUNTIME.dry_run:
        title += " - DRY-RUN"

    console.print(Panel.fit(
        f"[bold]{title}[/bold]\n\n"
        "This tool imports VMware VMs whose VMDKs are stored on an NFS share.\n"
        "The VMs are started directly from NFS first.\n"
        "The disks can then be migrated individually to the target storage.\n\n"
        f"Log file: {log_file}"
    ))

    if RUNTIME.nfs_root is None:
        nfs_root_input = questionary.text(
            "Path to the mounted NFS share:",
            default=str(DEFAULT_NFS_ROOT),
        ).ask()
        if nfs_root_input is None:
            raise KeyboardInterrupt
        nfs_root = Path(nfs_root_input)
    else:
        nfs_root = RUNTIME.nfs_root

    validate_nfs_root(nfs_root)

    vmx_files = discover_vms(nfs_root)
    if not vmx_files:
        console.print("[red]No .vmx files found.[/red]")
        sys.exit(1)

    choices = [
        questionary.Choice(title=str(path.relative_to(nfs_root)), value=path)
        for path in vmx_files
    ]

    selected_vmx = questionary.checkbox(
        "Which VMs should be imported?",
        choices=choices,
    ).ask()

    if not selected_vmx:
        console.print("[yellow]No VM selected.[/yellow]")
        sys.exit(0)

    storages = get_pve_storages()
    bridges = get_pve_bridges()
    used_vmids = get_used_vmids()

    if not storages:
        console.print("[red]No Proxmox storage found.[/red]")
        sys.exit(1)

    if not bridges:
        console.print("[red]No Proxmox bridges found.[/red]")
        sys.exit(1)

    plans: list[ImportPlan] = []
    for vmx_path in selected_vmx:
        vm = build_vm_from_vmx(vmx_path)
        plan = wizard_for_vm(vm, storages, bridges, used_vmids)
        plans.append(plan)

    show_plan(plans)
    show_disk_plan(plans)
    show_nic_plan(plans)

    confirm_or_abort("Plan so übernehmen und VM erstellen?", default=False)

    state_file = save_state(plans)
    console.print(f"[green]State saved:[/green] {state_file}")

    if RUNTIME.dry_run:
        console.print(Panel.fit(
            "[bold cyan]DRY-RUN aktiv[/bold cyan]\n\n"
            "No Proxmox VMs will be created, no descriptors written, "
            "no VMs started, and no disks migrated."
        ))

    for plan in plans:
        create_pve_vm(plan)

    start_approved_plans = start_vms(plans)
    show_vm_status(start_approved_plans if start_approved_plans else plans)

    if not start_approved_plans:
        console.print("[yellow]No VMs were approved to start. Disk migration is unavailable.[/yellow]")
        LOG.warning("No VMs were approved to start")
        console.print("[green]Done.[/green]")
        LOG.info("Finished without starting any VMs")
        return

    migration_mode = "skip"

    detached_runner = get_detach_runner()
    detached_title = (
        f"Yes, start in the background ({detached_runner})"
        if detached_runner
        else "Yes, start in the background (no runner found)"
    )

    if RUNTIME.auto_yes:
        migration_mode = "skip"
    else:
        migration_mode = questionary.select(
            "Start disk migration to target storage?",
            choices=[
                questionary.Choice(title="No, do not migrate now", value="skip"),
                questionary.Choice(title="Yes, run in the foreground", value="foreground"),
                questionary.Choice(title=detached_title, value="detached"),
            ],
            default="skip",
        ).ask()

        if migration_mode is None:
            raise KeyboardInterrupt

    if migration_mode == "foreground":
        move_disks_one_by_one(start_approved_plans)
        show_vm_status(start_approved_plans)

    elif migration_mode == "detached":
        start_disk_migration_detached(start_approved_plans)

    console.print("[green]Done.[/green]")
    LOG.info("Done")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled by user.[/yellow]")
        LOG.warning("Cancelled by user")
        sys.exit(130)
    except Exception as exc:
        console.print(f"\n[red]Error:[/red] {exc}")
        LOG.exception("Unhandled error")
        sys.exit(1)
