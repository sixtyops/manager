packer {
  required_plugins {
    qemu = {
      source  = "github.com/hashicorp/qemu"
      version = "~> 1"
    }
  }
}

variable "alpine_iso_url" {
  type    = string
  default = "https://dl-cdn.alpinelinux.org/alpine/v3.21/releases/x86_64/alpine-virt-3.21.3-x86_64.iso"
}

variable "alpine_iso_checksum" {
  type    = string
  default = "sha256:f28171c35bbf623aa3cbaec4b8b29297f13095b892c1a283b15970f7eb490f2d"
}

variable "app_version" {
  type    = string
  default = "latest"
}

variable "appliance_version" {
  type    = string
  default = "1.0"
}

variable "ghcr_image" {
  type    = string
  default = "ghcr.io/isolson/firmware-updater"
}

variable "recovery_secret" {
  type      = string
  sensitive = true
}

variable "ghcr_token" {
  type      = string
  sensitive = true
  default   = ""
}

variable "disk_size" {
  type    = string
  default = "8G"
}

variable "memory" {
  type    = number
  default = 1024
}

source "qemu" "tachyon" {
  iso_url          = var.alpine_iso_url
  iso_checksum     = var.alpine_iso_checksum
  output_directory = "output-tachyon"
  shutdown_command  = "poweroff"
  disk_size        = var.disk_size
  format           = "qcow2"
  accelerator      = "kvm"
  headless         = true
  memory           = var.memory
  cpus             = 2
  net_device       = "virtio-net"
  disk_interface   = "virtio"
  boot_wait        = "20s"
  boot_command = [
    "root<enter><wait5>",
    "ifconfig eth0 up && udhcpc -i eth0<enter><wait10>",
    "wget http://{{ .HTTPIP }}:{{ .HTTPPort }}/answers -O /tmp/answers<enter><wait5>",
    "wget http://{{ .HTTPIP }}:{{ .HTTPPort }}/install.sh -O /tmp/install.sh && sh /tmp/install.sh<enter>",
    "<wait300>"
  ]
  http_directory   = "${path.root}/http"
  ssh_username     = "root"
  ssh_password     = "tachyon-build"
  ssh_timeout      = "15m"
  ssh_file_transfer_method = "sftp"
  vm_name          = "tachyon-appliance"
}

build {
  sources = ["source.qemu.tachyon"]

  provisioner "file" {
    source      = "files/"
    destination = "/tmp/appliance-files/"
  }

  provisioner "shell" {
    scripts = [
      "scripts/01-base.sh",
      "scripts/02-docker.sh",
      "scripts/03-app.sh",
      "scripts/04-console.sh",
      "scripts/05-network.sh",
      "scripts/06-harden.sh",
      "scripts/07-cleanup.sh",
    ]
    environment_vars = [
      "APP_VERSION=${var.app_version}",
      "APPLIANCE_VERSION=${var.appliance_version}",
      "GHCR_IMAGE=${var.ghcr_image}",
      "RECOVERY_SECRET=${var.recovery_secret}",
      "GHCR_TOKEN=${var.ghcr_token}",
    ]
  }

}
