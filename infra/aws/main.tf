# Root config for the AWS k3s-on-EC2 substrate (ADR-010).
#
# Terraform owns the substrate ONLY. It provisions the network and (next
# increment) the k3s node, installs ArgoCD, and seeds the root app — then
# stops. ArgoCD owns everything in catalog/ (ADR-010 D4). Do not add the
# kubernetes/helm providers to manage in-cluster workloads here; that
# couples app lifecycle to TF state and hangs destroy.

module "network" {
  source = "../modules/network"

  name_prefix         = var.name_prefix
  vpc_cidr            = var.vpc_cidr
  public_subnet_count = var.public_subnet_count
}

# module "k3s_node" {          # next 9.1 increment
#   source            = "../modules/k3s-node"
#   name_prefix       = var.name_prefix
#   vpc_id            = module.network.vpc_id
#   subnet_id         = module.network.public_subnet_ids[0]
#   admin_source_cidr = var.admin_source_cidr   # 6443 + node admin, owner's IP only
#   ...
# }

# ArgoCD GitOps-Bridge handoff (install ArgoCD via Helm on the node,
# write the four knobs onto the cluster Secret, apply the root
# ApplicationSet) also lands in the next 9.1 increment, once the node
# module exists and its kubeconfig output is available.
