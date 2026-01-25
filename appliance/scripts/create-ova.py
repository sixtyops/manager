#!/usr/bin/env python3
"""Convert a QCOW2 disk image to OVA format (OVF + VMDK + manifest)."""

import argparse
import hashlib
import os
import subprocess
import sys
import tarfile
import textwrap


def convert_qcow2_to_vmdk(qcow2_path: str, vmdk_path: str) -> None:
    """Convert QCOW2 to stream-optimized VMDK."""
    print(f"Converting {qcow2_path} -> {vmdk_path}")
    subprocess.run(
        [
            "qemu-img", "convert",
            "-f", "qcow2",
            "-O", "vmdk",
            "-o", "subformat=streamOptimized",
            qcow2_path,
            vmdk_path,
        ],
        check=True,
    )


def parse_disk_size(disk_size: str) -> int:
    """Parse a disk size string (e.g., '8G', '16G', '8589934592') to bytes."""
    disk_size = disk_size.strip().upper()
    if disk_size.endswith("G"):
        return int(disk_size[:-1]) * 1024 * 1024 * 1024
    elif disk_size.endswith("M"):
        return int(disk_size[:-1]) * 1024 * 1024
    return int(disk_size)


def generate_ovf(
    vmdk_filename: str,
    vmdk_size: int,
    name: str,
    version: str,
    cpus: int = 2,
    memory_mb: int = 1024,
    disk_capacity_bytes: int = 8589934592,
) -> str:
    """Generate OVF descriptor XML.

    Raises ValueError if hardware parameters are out of valid range.
    """
    if cpus < 1 or cpus > 16:
        raise ValueError(f"cpus must be between 1 and 16, got {cpus}")
    if memory_mb < 256 or memory_mb > 65536:
        raise ValueError(f"memory_mb must be between 256 and 65536, got {memory_mb}")
    if disk_capacity_bytes < 1073741824:  # 1 GB
        raise ValueError(f"disk_capacity_bytes must be at least 1GB (1073741824), got {disk_capacity_bytes}")
    return textwrap.dedent(f"""\
    <?xml version="1.0" encoding="UTF-8"?>
    <Envelope xmlns="http://schemas.dmtf.org/ovf/envelope/1"
              xmlns:cim="http://schemas.dmtf.org/wbem/wscim/1/common"
              xmlns:ovf="http://schemas.dmtf.org/ovf/envelope/1"
              xmlns:rasd="http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData"
              xmlns:vmw="http://www.vmware.com/schema/ovf"
              xmlns:vssd="http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_VirtualSystemSettingData">
      <References>
        <File ovf:href="{vmdk_filename}" ovf:id="file1" ovf:size="{vmdk_size}"/>
      </References>
      <DiskSection>
        <Info>Virtual disk information</Info>
        <Disk ovf:capacity="{disk_capacity_bytes}" ovf:capacityAllocationUnits="byte"
              ovf:diskId="vmdisk1" ovf:fileRef="file1"
              ovf:format="http://www.vmware.com/interfaces/specifications/vmdk.html#streamOptimized"/>
      </DiskSection>
      <NetworkSection>
        <Info>The list of logical networks</Info>
        <Network ovf:name="bridged">
          <Description>Bridged network</Description>
        </Network>
      </NetworkSection>
      <VirtualSystem ovf:id="{name}">
        <Info>SixtyOps Appliance</Info>
        <Name>{name}</Name>
        <AnnotationSection>
          <Info>Appliance description</Info>
          <Annotation>SixtyOps Manager v{version} - Automated firmware management for wireless network devices.</Annotation>
        </AnnotationSection>
        <OperatingSystemSection ovf:id="101">
          <Info>The operating system</Info>
          <Description>Linux 64-Bit</Description>
        </OperatingSystemSection>
        <VirtualHardwareSection>
          <Info>Virtual hardware requirements</Info>
          <System>
            <vssd:ElementName>Virtual Hardware Family</vssd:ElementName>
            <vssd:InstanceID>0</vssd:InstanceID>
            <vssd:VirtualSystemType>vmx-14</vssd:VirtualSystemType>
          </System>
          <Item>
            <rasd:AllocationUnits>hertz * 10^6</rasd:AllocationUnits>
            <rasd:Description>Number of Virtual CPUs</rasd:Description>
            <rasd:ElementName>{cpus} virtual CPU(s)</rasd:ElementName>
            <rasd:InstanceID>1</rasd:InstanceID>
            <rasd:ResourceType>3</rasd:ResourceType>
            <rasd:VirtualQuantity>{cpus}</rasd:VirtualQuantity>
          </Item>
          <Item>
            <rasd:AllocationUnits>byte * 2^20</rasd:AllocationUnits>
            <rasd:Description>Memory Size</rasd:Description>
            <rasd:ElementName>{memory_mb}MB of memory</rasd:ElementName>
            <rasd:InstanceID>2</rasd:InstanceID>
            <rasd:ResourceType>4</rasd:ResourceType>
            <rasd:VirtualQuantity>{memory_mb}</rasd:VirtualQuantity>
          </Item>
          <Item>
            <rasd:Description>SCSI Controller</rasd:Description>
            <rasd:ElementName>SCSI Controller 0</rasd:ElementName>
            <rasd:InstanceID>3</rasd:InstanceID>
            <rasd:ResourceSubType>lsilogic</rasd:ResourceSubType>
            <rasd:ResourceType>6</rasd:ResourceType>
          </Item>
          <Item>
            <rasd:AddressOnParent>0</rasd:AddressOnParent>
            <rasd:ElementName>Hard Disk 1</rasd:ElementName>
            <rasd:HostResource>ovf:/disk/vmdisk1</rasd:HostResource>
            <rasd:InstanceID>4</rasd:InstanceID>
            <rasd:Parent>3</rasd:Parent>
            <rasd:ResourceType>17</rasd:ResourceType>
          </Item>
          <Item>
            <rasd:AutomaticAllocation>true</rasd:AutomaticAllocation>
            <rasd:Connection>bridged</rasd:Connection>
            <rasd:Description>VMXNET3 ethernet adapter on bridged</rasd:Description>
            <rasd:ElementName>Ethernet adapter 1</rasd:ElementName>
            <rasd:InstanceID>5</rasd:InstanceID>
            <rasd:ResourceSubType>VMXNET3</rasd:ResourceSubType>
            <rasd:ResourceType>10</rasd:ResourceType>
          </Item>
        </VirtualHardwareSection>
      </VirtualSystem>
    </Envelope>
    """)


def sha256_file(path: str) -> str:
    """Compute SHA256 hash of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(8192)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def create_ova(
    qcow2_path: str,
    output_path: str,
    name: str = "sixtyops-appliance",
    version: str = "latest",
    cpus: int = 2,
    memory_mb: int = 1024,
    disk_capacity_bytes: int = 8589934592,
) -> None:
    """Create OVA from QCOW2 image."""
    work_dir = os.path.dirname(output_path) or "."

    vmdk_filename = f"{name}.vmdk"
    ovf_filename = f"{name}.ovf"
    mf_filename = f"{name}.mf"

    vmdk_path = os.path.join(work_dir, vmdk_filename)
    ovf_path = os.path.join(work_dir, ovf_filename)
    mf_path = os.path.join(work_dir, mf_filename)

    try:
        # Step 1: Convert QCOW2 to VMDK
        convert_qcow2_to_vmdk(qcow2_path, vmdk_path)
        vmdk_size = os.path.getsize(vmdk_path)

        # Step 2: Generate OVF descriptor
        ovf_content = generate_ovf(
            vmdk_filename, vmdk_size, name, version,
            cpus=cpus, memory_mb=memory_mb,
            disk_capacity_bytes=disk_capacity_bytes,
        )
        with open(ovf_path, "w") as f:
            f.write(ovf_content)
        print(f"Generated OVF descriptor: {ovf_path}")

        # Step 3: Generate manifest with SHA256 checksums
        ovf_hash = sha256_file(ovf_path)
        vmdk_hash = sha256_file(vmdk_path)
        manifest = (
            f"SHA256({ovf_filename})= {ovf_hash}\n"
            f"SHA256({vmdk_filename})= {vmdk_hash}\n"
        )
        with open(mf_path, "w") as f:
            f.write(manifest)
        print(f"Generated manifest: {mf_path}")

        # Step 4: Bundle as TAR (OVA)
        # OVF spec requires OVF first, then manifest, then disk
        print(f"Creating OVA: {output_path}")
        with tarfile.open(output_path, "w") as tar:
            tar.add(ovf_path, arcname=ovf_filename)
            tar.add(mf_path, arcname=mf_filename)
            tar.add(vmdk_path, arcname=vmdk_filename)

        ova_size_mb = os.path.getsize(output_path) / (1024 * 1024)
        print(f"OVA created successfully: {output_path} ({ova_size_mb:.1f} MB)")

    finally:
        # Clean up intermediate files
        for path in [vmdk_path, ovf_path, mf_path]:
            if os.path.exists(path):
                os.remove(path)


def main():
    parser = argparse.ArgumentParser(description="Convert QCOW2 to OVA")
    parser.add_argument("qcow2", help="Input QCOW2 image path")
    parser.add_argument(
        "-o", "--output",
        help="Output OVA path (default: sixtyops-appliance.ova)",
        default="sixtyops-appliance.ova",
    )
    parser.add_argument(
        "-n", "--name",
        help="Appliance name (default: sixtyops-appliance)",
        default="sixtyops-appliance",
    )
    parser.add_argument(
        "-v", "--version",
        help="Appliance version string",
        default="latest",
    )
    parser.add_argument(
        "--cpus",
        help="Number of virtual CPUs (default: 2)",
        type=int,
        default=2,
    )
    parser.add_argument(
        "--memory",
        help="Memory in MB (default: 1024)",
        type=int,
        default=1024,
    )
    parser.add_argument(
        "--disk-size",
        help="Disk size (e.g., '8G', '16G', default: 8G)",
        default="8G",
    )
    args = parser.parse_args()

    if not os.path.exists(args.qcow2):
        print(f"Error: Input file not found: {args.qcow2}", file=sys.stderr)
        sys.exit(1)

    # Check for qemu-img
    try:
        subprocess.run(["qemu-img", "--version"], capture_output=True, check=True)
    except FileNotFoundError:
        print("Error: qemu-img not found. Install qemu-utils.", file=sys.stderr)
        sys.exit(1)

    disk_bytes = parse_disk_size(args.disk_size)
    create_ova(
        args.qcow2, args.output, args.name, args.version,
        cpus=args.cpus, memory_mb=args.memory,
        disk_capacity_bytes=disk_bytes,
    )


if __name__ == "__main__":
    main()
