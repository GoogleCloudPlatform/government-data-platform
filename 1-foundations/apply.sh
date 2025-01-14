#!/bin/bash

PARENT_DIR=$(dirname "$(dirname "$(realpath "$0")")")
TF_ACTIVE_WORKSPACE=$(terraform workspace show)
CONFIG_DIR="$PARENT_DIR/config/$TF_ACTIVE_WORKSPACE"

if [ -z "$TF_ACTIVE_WORKSPACE" ]; then
  echo "No active Terraform workspace found. Please run 'terraform workspace select <workspace>' first."
  exit 1
fi

echo "Terraform selected workspace: $TF_ACTIVE_WORKSPACE"

REQUIRED_CONFIG_FILES=(
  "$CONFIG_DIR/config.yml"
  "$CONFIG_DIR/backend.conf"
  "$CONFIG_DIR/workflows.json"
)

for file in "${file_paths[@]}"; do
  if [ ! -e "$file" ]; then
    echo "Required config file not found: $file"
    exit 1
  fi
done

terraform init -reconfigure -backend-config="$CONFIG_DIR/backend.conf"

if [[ $1 = "--destroy" ]]; then
  terraform destroy
  exit
fi

if [[ -f "$CONFIG_DIR/terraform.tfvars" ]]; then
  terraform plan -out=tfplanfile -var-file=$CONFIG_DIR/terraform.tfvars -lock=false || exit $?
else 
  terraform plan -out=tfplanfile -lock=false || exit $?
fi

read -p "Proceed? (y/N) " yn
case $yn in
  [yY] ) echo "Continuing with apply..."
    ;;
  [nN] ) echo "Aborted."
    exit;;
  * ) echo "Aborted."
    exit;;
esac

terraform apply tfplanfile

