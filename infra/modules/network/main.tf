# Network for the cost-min k3s-on-EC2 substrate (ADR-010 D1/D2).
#
# Public-subnet design ON PURPOSE: the cluster node's inbound path (443)
# is served by a bundled ingress, so there is no NAT gateway (ADR-010's
# ~$33/mo NAT saving). The node's inbound surface is locked by the
# k3s-node module's security group, NOT by being private. The trust VMs'
# OUTBOUND egress is priced separately as one public IP each (ADR-010 D2)
# — this module does not create them.

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  az_names = slice(data.aws_availability_zones.available.names, 0, var.public_subnet_count)
}

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-vpc"
  })
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-igw"
  })
}

resource "aws_subnet" "public" {
  count = var.public_subnet_count

  vpc_id                  = aws_vpc.this.id
  cidr_block              = cidrsubnet(var.vpc_cidr, var.subnet_newbits, count.index)
  availability_zone       = local.az_names[count.index]
  map_public_ip_on_launch = true

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-public-${local.az_names[count.index]}"
    # Hint for any in-cluster LoadBalancer controller that this subnet is
    # public-facing (harmless on k3s; load-bearing if the EKS module is
    # ever built).
    "kubernetes.io/role/elb" = "1"
  })
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-public-rt"
  })
}

resource "aws_route_table_association" "public" {
  count = var.public_subnet_count

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}
