variable "region" {
  description = "AWS region. Cost figures in ADR-010 are us-east-1; NAT/egress roughly double in AP/SA."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Prefix for resource names and tags."
  type        = string
  default     = "homelab"
}

variable "vpc_cidr" {
  description = "VPC CIDR. Must not overlap Cilium (10.42.0.0/16) or k3s service (10.43.0.0/16) ranges — ADR-010 D3.4."
  type        = string
  default     = "10.0.0.0/16"
}

variable "public_subnet_count" {
  description = "Public subnets, one per AZ. 1 for the cost-min tier; 2+ enables the multi-AZ HA sizing profiles."
  type        = number
  default     = 2
}
