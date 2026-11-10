# Vaults Required by the Playbooks

Vault files live under `/work/migrate-v2v/vaults/` by default. Set
`MIGRATE_V2V_VAULT_DIR` to use a different directory. The filenames below are
relative to that directory.

## Editing a Vault

`bin/aws-vault.sh`, `bin/azure-vault.sh`, `bin/gcp-vault.sh`,
`bin/openstack-vault.sh`, and `bin/vsphere-vault.sh` wrap `ansible-vault edit`/`create
--encrypt-vault-id=default` against the matching file below, creating it if it
doesn't exist yet.

```sh
bin/aws-vault.sh
```

Set `VAULT` to point at a different file (e.g. a per-environment vault):

```sh
VAULT=/work/migrate-v2v/vaults/aws-staging.yml bin/aws-vault.sh
```

## Credentials for the bin/ Tools

The playbooks read only the vaults. The standalone reconcile tools
(`reconcile-catalog-*.py`) and `enable-cbt.py` follow the usual
command-line > environment > config-file convention, with cached logins
last:

1. An explicit command-line choice: `--profile` (AWS), `--credentials-file`
   (GCP), `--cloud` (OpenStack).
2. Credential variables exported in the shell: `AWS_PROFILE`,
   `AWS_ACCESS_KEY_ID`, or `AWS_WEB_IDENTITY_TOKEN_FILE`; `AZURE_TENANT`,
   `AZURE_CLIENT_ID`, and `AZURE_SECRET` (or the SDK's `*_ID`/`CLIENT_SECRET`
   spellings); `GOOGLE_APPLICATION_CREDENTIALS`; `OS_CLOUD` or `OS_AUTH_URL`
   (with the rest of `OS_*`); `VCENTER_USERNAME` and `VCENTER_PASSWORD`.
3. The vault entry for the account, subscription, project, or vCenter in
   scope, matched the way the playbooks match it.
4. Cached logins: the default `~/.aws` profile or an instance profile,
   `az login` or managed identity, the gcloud application-default login.
   OpenStack has no such fallback and stops with a message instead.

So a shell with nothing exported uses the vault, and exporting credentials
overrides it for that shell without editing the vault. The vault is
decrypted only when steps 1 and 2 did not decide, so a user working from
exported credentials is never asked for the vault password; one that exists
but cannot be decrypted stops the run rather than falling through to a cached
login. The password comes from `VAULT_PASSWORD_FILE`, else
`~/.dib-vault-pass`, else an interactive prompt.

Each reconcile prints the source it used, for example `(credentials: vault
entry 'lab')` or `(credentials: AZURE_* environment; azure.yml not
consulted)`; the second form is the cue to check for a stale export. They
also refuse to run when the credentials belong to a different account,
subscription, or project than the catalog scope. `--vault` names another
vault file.

The `*-project-facts.py` scripts feed the launch dialogs, so they select the
same vault entry the launch playbook will use; only
`openstack-project-facts.py` with no PROJECT argument uses `OS_*` instead.

## AWS Vault

aws.yml

```yaml
aws_projects:
  "123456789012":
    aws_account_id: "123456789012"
    aws_region: "my-region"
    s3_bucket: "my-bucket"
    vmimport_role_name: "vmimport-role"
    # Optional: aws_profile, aws_access_key_id,
    # aws_secret_access_key, aws_session_token
```

Select an account with `-e aws_target_project=<key-or-account-id>`. A
single-entry map is auto-selected. A legacy flat mapping containing
`aws_region`, `s3_bucket`, and `vmimport_role_name` remains supported.

`bin/aws-project-facts.py` reads `aws.yml` and selects an account
the same way the playbooks do (vault key, account ID, or profile).
Without exported AWS credentials, `bin/reconcile-catalog-aws.py` uses the
entry whose key or `aws_account_id` matches the catalog's account (see
[Credentials for the bin/ Tools](#credentials-for-the-bin-tools)).

## GCP Vault

gcp.yml

```yaml
gcp_projects:
  my-project:
    gcp_project: "my-project"
    gcs_bucket: "my-bucket"
    gcp_import_location: "us-central1"
    service_account_key: |
      {
        "type": "service_account",
        "project_id": "my-project",
        "client_email": "you@my-project.iam.gserviceaccount.com"
      }
```

Select a project with `-e gcp_target_project=<key-or-project-id>`. A
single-entry map is auto-selected. A legacy flat mapping containing
`gcp_project`, `gcs_bucket`, optional `gcp_import_location`, and
`service_account_key` remains supported.

`bin/gcp-project-facts.py` reads `gcp.yml` and selects a project
the same way the playbooks do (vault key, `gcp_project`, or `project_id`).

### `gcp_import_location`

Optional; `us-central1` when the entry leaves it out. The region the Migrate
to Virtual Machines _import job_ runs in - the value
passed to `gcloud compute migration image-imports --location`. It is easy to
misread this as a storage setting, so, concretely, it does **not** control:

- **Where `gcs_bucket` lives.** A bucket's location is fixed when the bucket
  is created and is not settable from here. The import reads the staged VMDK
  across regions perfectly well when the two differ.
- **Where the finished image lives.** A GCE Compute image is a _global_
  resource, so the imported image is equally usable from every region no
  matter which one converted it.

So this is purely a choice of which region does the conversion work, and it
can be changed freely - including to route around a regional outage:

```sh
./bin/migrate-gcp-vm.sh -e vm_name=my-vm -e gcp_import_location=us-east1
```

On 2026-08-12 the import service in `us-central1` failed four consecutive
times at the `loadingSourceFiles` step with `code 13`, "Internal migration
service error", while the byte-identical staged object imported cleanly in
`us-east1`. If an import fails with an internal error and no useful detail,
retrying in another region is the fastest way to tell a bad artifact apart
from a bad region.

## OpenStack Vault

openstack.yml

```txt
openstack_projects:
  my-project-a:
    auth_url: "https://keystone.example.com:5000/v3"
    username: "my-username"
    password: "my-password"
    project_name: "my-project-a"
    project_id: "project-id-a"
    region_name: "my-region"
    user_domain_name: "Default"
    project_domain_name: "Default"
  my-project-b:
    auth_url: "https://keystone.example.com:5000/v3"
    username: "my-username"
    password: "my-password"
    project_name: "my-project-b"
    project_id: "project-id-b"
    region_name: "my-region"
    user_domain_name: "Default"
    project_domain_name: "Default"

# A legacy single-project openstack_auth mapping remains supported.
```

A vault may contain either the legacy single `openstack_auth` mapping or an
`openstack_projects` mapping. Select a project with
`-e openstack_target_project=<vault-key-or-project-name-or-project-id>`; a
single-entry map is selected automatically. Quote numeric project names and IDs.
`region_name` should be set for multi-region clouds and is recorded on the
image catalog entry.

Without `--cloud` or exported `OS_*` credentials,
`bin/reconcile-catalog-openstack.py` uses the entry whose key,
`project_name`, or `project_id` matches the catalog's project, and only that
entry (no `clouds.yaml` values are mixed in). See
[Credentials for the bin/ Tools](#credentials-for-the-bin-tools).

`bin/openstack-project-facts.py` also reads `openstack.yml` and selects a
project the same way the playbooks do (vault key, `project_name`, or
`project_id`). See
[OpenStack Launch Networking](../README.md#openstack-launch-networking).

## vSphere Vault

`vsphere.yml`

```yaml
vsphere_projects:
  example:
    vcenter_hostname: "my-vcenter"
    vcenter_username: "my-admin-user"
    vcenter_password: "my-admin-password"
    datacenter: "Datacenter"
    validate_certs: true
    # Optional guest SSH login, used only by bin/stage-v2v.sh
    ssh_username: "root"
    ssh_password: "my-guest-password"
    ssh_private_key_file: "~/.ssh/id_ed25519"
  other-site:
    vcenter_hostname: "other-vcenter"
    vcenter_username: "my-admin-user"
    vcenter_password: "other-password"
    datacenter: "Other-Datacenter"
    validate_certs: true

# A legacy flat vcenter_hostname/vcenter_username/vcenter_password mapping
# remains supported.
```

Unless `VCENTER_USERNAME` and `VCENTER_PASSWORD` are exported,
`bin/enable-cbt.py` selects its vCenter the same way (`--vcenter
<key-or-hostname>`), taking the datacenter and `validate_certs` from the
entry.

Select a vCenter with `-e vsphere_target_vcenter=<key-or-hostname>`. A
single-entry map is auto-selected. The optional guest-login `ssh_*` keys may
also be set flat at the top level or with `-e`, which override the selected
entry; unset values fall back to the inventory, ssh-agent, or `~/.ssh/config`.
Each migration catalog row records the selected vCenter key as
`source_vcenter_key` (or `legacy` for the flat mapping) alongside
`source_vcenter`, so a row names the vCenter it came from.

## Azure Vault

azure.yml

```yaml
azure_projects:
  my-subscription:
    tenant_id: "00000000-0000-0000-0000-000000000000"
    subscription_id: "11111111-1111-1111-1111-111111111111"
    client_id: "22222222-2222-2222-2222-222222222222"
    client_secret: "client-secret"
    resource_group: "my-rg"
    location: "westus2"
    storage_account: "mystorageaccount"
    # Optional: storage_container (defaults to migrate-v2v-images)
```

Each entry holds one service principal and where its migration lands: the
subscription the principal works in, the resource group the managed images
are created in (it must already exist), the location for images and the
staging storage account, and the storage account VHDs are staged in (3-24
lowercase letters and digits; created when missing, used as-is when it
exists). Select a subscription with `-e
azure_target_project=<vault-key-or-subscription-id>`; a single-entry map is
auto-selected. This is the same file and schema `dib7` uses, so the two
repositories can share one vault.

Credentials reach the `azure.azcollection` modules through `module_defaults`,
not the environment, so the secret never appears in `ps` during an upload.
`bin/reconcile-catalog-azure.py` and `bin/azure-project-facts.py` decrypt
this vault and use the matching entry's service principal, so a personal
`az login` (which Entra ID security defaults may block) is not needed.
Exported `AZURE_TENANT`/`AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, and
`AZURE_SECRET`/`AZURE_CLIENT_SECRET` override the vault for
`reconcile-catalog-azure.py`; `az login` is tried only when neither has
credentials for the subscription.
