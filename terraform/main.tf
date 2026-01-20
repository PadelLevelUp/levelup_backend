provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
}

data "google_compute_network" "default" {
  name = "default"
}

resource "google_compute_firewall" "http-https" {
  name    = "levelup-allow-http-https"
  network = data.google_compute_network.default.name

  allow {
    protocol = "tcp"
    ports    = ["80", "443"]
  }

  source_ranges = ["0.0.0.0/0"]
}

resource "google_compute_firewall" "allow-postgres" {
  name    = "levelup-allow-postgres"
  network = data.google_compute_network.default.name

  allow {
    protocol = "tcp"
    ports    = ["5432"]
  }

  #source_ranges = ["2.80.118.223/32"]
  source_ranges = ["0.0.0.0/0"]
  description   = "Allow external access to PostgreSQL"
}

resource "google_compute_address" "static_ip" {
  name = "levelup-static-ip"
}

resource "google_compute_instance" "levelup" {
  name         = "levelup-instance"
  machine_type = "e2-micro"
  zone         = var.zone

  allow_stopping_for_update = true

  boot_disk {
    initialize_params {
      image = "debian-cloud/debian-11"
      size  = 10
    }
  }

  network_interface {
    network = data.google_compute_network.default.name
    access_config {
      nat_ip = google_compute_address.static_ip.address
    }
  }

  service_account {
    email  = google_service_account.vm_sa.email
    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
  }

  metadata_startup_script = <<-EOT
    #!/bin/bash
    set -euxo pipefail

    apt-get update -y
    apt-get install -y docker.io

    systemctl enable docker
    systemctl start docker

    mkdir -p /data/postgres
    chmod 700 /data/postgres

    if [ ! "$(docker ps -a -q -f name=postgres)" ]; then
      docker run -d \
        --name postgres \
        -e POSTGRES_USER=padel_app_user \
        -e POSTGRES_PASSWORD=${var.postgres_password} \
        -e POSTGRES_DB=padel_app \
        -p 5432:5432 \
        -v /data/postgres:/var/lib/postgresql/data \
        postgres:15
    else
      docker start postgres
    fi
  EOT

  tags = ["http-server", "https-server"]
}

resource "google_storage_bucket" "general" {
  name     = "padel-levelup-2026-storage"
  location = "europe-west1"
  storage_class = "NEARLINE"

  force_destroy = true
  uniform_bucket_level_access = true
}

resource "google_service_account" "vm_sa" {
  account_id   = "levelup-vm-sa"
  display_name = "Padel App VM SA"
}

resource "google_storage_bucket_iam_member" "allow_instance_uploads" {
  bucket = google_storage_bucket.general.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.vm_sa.email}"
}

resource "google_storage_bucket_iam_member" "public_all" {
  bucket = google_storage_bucket.general.name
  role   = "roles/storage.objectViewer"
  member = "allUsers"
}