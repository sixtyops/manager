"""Tests for appliance/scripts/create-ova.py."""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

# Add appliance/scripts to path so we can import create-ova
sys.path.insert(0, str(Path(__file__).parent.parent / "appliance" / "scripts"))
import importlib
create_ova = importlib.import_module("create-ova")


class TestParseDiskSize:
    def test_gigabytes(self):
        assert create_ova.parse_disk_size("8G") == 8 * 1024 * 1024 * 1024

    def test_megabytes(self):
        assert create_ova.parse_disk_size("512M") == 512 * 1024 * 1024

    def test_raw_bytes(self):
        assert create_ova.parse_disk_size("8589934592") == 8589934592

    def test_case_insensitive(self):
        assert create_ova.parse_disk_size("8g") == 8 * 1024 * 1024 * 1024
        assert create_ova.parse_disk_size("512m") == 512 * 1024 * 1024

    def test_whitespace_stripped(self):
        assert create_ova.parse_disk_size("  8G  ") == 8 * 1024 * 1024 * 1024

    def test_sixteen_gig(self):
        assert create_ova.parse_disk_size("16G") == 16 * 1024 * 1024 * 1024

    def test_one_gig(self):
        assert create_ova.parse_disk_size("1G") == 1024 * 1024 * 1024

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            create_ova.parse_disk_size("abc")


class TestGenerateOVF:
    def _parse_ovf(self, **kwargs):
        defaults = {
            "vmdk_filename": "test.vmdk",
            "vmdk_size": 1000000,
            "name": "test-appliance",
            "version": "1.0.0",
        }
        defaults.update(kwargs)
        xml_str = create_ova.generate_ovf(**defaults)
        return ET.fromstring(xml_str)

    def test_valid_xml(self):
        root = self._parse_ovf()
        assert root is not None

    def test_default_cpu_count(self):
        root = self._parse_ovf()
        ns = {"rasd": "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData"}
        # ResourceType 3 = CPU
        for item in root.iter("{http://schemas.dmtf.org/ovf/envelope/1}Item"):
            rt = item.find("rasd:ResourceType", ns)
            if rt is not None and rt.text == "3":
                qty = item.find("rasd:VirtualQuantity", ns)
                assert qty.text == "2"
                return
        pytest.fail("CPU item not found in OVF")

    def test_custom_cpu_count(self):
        root = self._parse_ovf(cpus=4)
        ns = {"rasd": "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData"}
        for item in root.iter("{http://schemas.dmtf.org/ovf/envelope/1}Item"):
            rt = item.find("rasd:ResourceType", ns)
            if rt is not None and rt.text == "3":
                qty = item.find("rasd:VirtualQuantity", ns)
                assert qty.text == "4"
                return
        pytest.fail("CPU item not found in OVF")

    def test_default_memory(self):
        root = self._parse_ovf()
        ns = {"rasd": "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData"}
        # ResourceType 4 = Memory
        for item in root.iter("{http://schemas.dmtf.org/ovf/envelope/1}Item"):
            rt = item.find("rasd:ResourceType", ns)
            if rt is not None and rt.text == "4":
                qty = item.find("rasd:VirtualQuantity", ns)
                assert qty.text == "1024"
                return
        pytest.fail("Memory item not found in OVF")

    def test_custom_memory(self):
        root = self._parse_ovf(memory_mb=2048)
        ns = {"rasd": "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData"}
        for item in root.iter("{http://schemas.dmtf.org/ovf/envelope/1}Item"):
            rt = item.find("rasd:ResourceType", ns)
            if rt is not None and rt.text == "4":
                qty = item.find("rasd:VirtualQuantity", ns)
                assert qty.text == "2048"
                return
        pytest.fail("Memory item not found in OVF")

    def test_default_disk_capacity(self):
        root = self._parse_ovf()
        ns = {"ovf": "http://schemas.dmtf.org/ovf/envelope/1"}
        disk = root.find(".//ovf:Disk", ns)
        assert disk is not None
        assert disk.get("{http://schemas.dmtf.org/ovf/envelope/1}capacity") == "8589934592"

    def test_custom_disk_capacity(self):
        cap = 16 * 1024 * 1024 * 1024  # 16GB
        root = self._parse_ovf(disk_capacity_bytes=cap)
        ns = {"ovf": "http://schemas.dmtf.org/ovf/envelope/1"}
        disk = root.find(".//ovf:Disk", ns)
        assert disk.get("{http://schemas.dmtf.org/ovf/envelope/1}capacity") == str(cap)

    def test_nic_type_vmxnet3(self):
        root = self._parse_ovf()
        ns = {"rasd": "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData"}
        # ResourceType 10 = Ethernet
        for item in root.iter("{http://schemas.dmtf.org/ovf/envelope/1}Item"):
            rt = item.find("rasd:ResourceType", ns)
            if rt is not None and rt.text == "10":
                subtype = item.find("rasd:ResourceSubType", ns)
                assert subtype.text == "VMXNET3"
                return
        pytest.fail("Ethernet adapter not found in OVF")

    def test_vmdk_reference(self):
        root = self._parse_ovf(vmdk_filename="myimage.vmdk", vmdk_size=999)
        ns = {"ovf": "http://schemas.dmtf.org/ovf/envelope/1"}
        file_ref = root.find(".//ovf:File", ns)
        assert file_ref.get("{http://schemas.dmtf.org/ovf/envelope/1}href") == "myimage.vmdk"
        assert file_ref.get("{http://schemas.dmtf.org/ovf/envelope/1}size") == "999"

    def test_appliance_name_and_version(self):
        xml_str = create_ova.generate_ovf("t.vmdk", 100, "my-app", "2.5.0")
        assert "my-app" in xml_str
        assert "v2.5.0" in xml_str
