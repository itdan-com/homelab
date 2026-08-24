variable "name_prefix" {
  description = "Prefix for resource names and the Name tag (e.g. \"homelab\")."
  type        = string
  default     = "homelab"
}

variable "vpc_cidr" {
  description = <<-EOT
    CIDR block for the VPC. MUST NOT overlap the Cilium pod CIDR
    (10.42.0.0/16) or the k3s service CIDR (10.43.0.0/16) — ADR-010 D3.4
    / ADR-003. A collision silently breaks pod networking on the node.
  EOT
  type        = string
  default     = "10.0.0.0/16"

  validation {
    condition     = can(cidrnetmask(var.vpc_cidr))
    error_message = "vpc_cidr must be a valid IPv4 CIDR block (e.g. 10.0.0.0/16)."
  }

  validation {
    condition     = !startswith(var.vpc_cidr, "10.42.") && !startswith(var.vpc_cidr, "10.43.")
    error_message = "vpc_cidr must not overlap Cilium pod CIDR (10.42.0.0/16) or k3s service CIDR (10.43.0.0/16) — ADR-010 D3.4."
  }
}

variable "public_subnet_count" {
  description = "Number of public subnets, one per AZ. The cost-min tier uses the node in one; >1 is for the HA sizing profiles."
  type        = number
  default     = 2

  validation {
    condition     = var.public_subnet_count >= 1 && var.public_subnet_count <= 6
    error_message = "public_subnet_count must be between 1 and 6."
  }
}

variable "subnet_newbits" {
  description = "Bits to add to the VPC prefix for each subnet. 8 turns a /16 into /24 subnets."
  type        = number
  default     = 8
}

variable "tags" {
  description = "Tags applied to every resource in this module."
  type        = map(string)
  default     = {}
}
