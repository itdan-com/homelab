provider "aws" {
  region = var.region

  # Every resource this root creates gets these tags, so a stray billable
  # resource is always traceable to the platform and phase (destroy-leak
  # hunting in 9.5 keys off them).
  default_tags {
    tags = {
      Project   = "homelab"
      ManagedBy = "terraform"
      Phase     = "9"
      Component = "substrate"
    }
  }
}
