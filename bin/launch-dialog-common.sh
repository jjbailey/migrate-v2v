#!/bin/bash
# bin/launch-dialog-common.sh
# vim: set tabstop=4 shiftwidth=4 expandtab:

# Shared UI for the AWS, Azure, GCP, and OpenStack launch and migrate dialogs. A
# caller sets DIALOG_NAME, then sources this file. Every widget aborts the
# dialog on cancel or Esc, and nothing runs until finish() confirms it.

set -euo pipefail

ARTIFACT_ROOT=${ARTIFACT_ROOT:-/work/migrate-v2v}
export ARTIFACT_ROOT
MIGRATE_V2V_CATALOG_DIR=${MIGRATE_V2V_CATALOG_DIR:-$ARTIFACT_ROOT/catalogs}
export MIGRATE_V2V_CATALOG_DIR
MIGRATE_V2V_VAULT_DIR=${MIGRATE_V2V_VAULT_DIR:-$ARTIFACT_ROOT/vaults}
export MIGRATE_V2V_VAULT_DIR

# dialog and whiptail both draw on stdout and write the answer on stderr, so
# dlg() swaps the two to let $(dlg ...) capture the answer.
if command -v dialog > /dev/null 2>&1 ; then
    DIALOG=(dialog --clear)
elif command -v whiptail > /dev/null 2>&1 ; then
    DIALOG=(whiptail --clear)
else
    echo "${DIALOG_NAME:-launch-dialog}: install dialog or whiptail" >&2
    exit 1
fi

if [ ! -t 0 ] || [ ! -t 1 ] || [ ! -t 2 ] ; then
    echo "${DIALOG_NAME}: an interactive terminal is required." >&2
    echo "Run it directly, without a pipe or a redirect." >&2
    exit 1
fi

# Match the erase character to the Backspace key the terminal advertises, then
# put the operator's settings back however this script exits.
tty_state=""
restore_tty()
{
    if [ -n "$tty_state" ] ; then
        stty "$tty_state" < /dev/tty 2> /dev/null || true
    fi
}
if tty_state=$(stty -g < /dev/tty 2> /dev/null) ; then
    backspace_key=$(tput kbs 2> /dev/null || true)
    if [ "${#backspace_key}" -eq 1 ] ; then
        stty erase "$backspace_key" < /dev/tty 2> /dev/null || true
    fi
    trap restore_tty EXIT
fi

dlg()
{
    "${DIALOG[@]}" "$@" 3>&1 1>&2 2>&3
}

rows=$(tput lines 2> /dev/null || echo 24)
cols=$(tput cols 2> /dev/null || echo 80)
case "$rows" in '' | *[!0-9]*) rows=24 ;; esac
case "$cols" in '' | *[!0-9]*) cols=80 ;; esac
if [ "$cols" -lt 20 ] || [ "$rows" -lt 10 ] ; then
    echo "${DIALOG_NAME}: terminal is too small (${cols}x${rows}) ; minimum is 20x10." >&2
    exit 1
fi
COLS=$((cols - 4))
[ "$COLS" -le 92 ] || COLS=92

height()
{
    local h=$1
    [ "$h" -ge 8 ] || h=8
    [ "$h" -le "$((rows - 2))" ] || h=$((rows - 2))
    printf %s "$h"
}

# abort exits the script, so only call it from the top level. A helper that
# runs inside $(...) returns non-zero instead, and its caller does || abort.
abort()
{
    dlg --msgbox "${ACTION_VERB:-Launch} cancelled. Nothing was run." 7 "$COLS" || true
    exit 0
}

input()
{
    local value
    value=$(dlg --inputbox "$1" "$(height 9)" "$COLS" "$2") || return $?
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    printf %s "$value"
}

input_required()
{
    local prompt=$1 default=$2 message=$3 value
    while : ; do
        value=$(input "$prompt" "$default") || return 1
        if [ -n "$value" ] ; then
            printf %s "$value"
            return 0
        fi
        bad "$message" || true
    done
}

yesno()
{
    local a=()
    local status
    [ "$2" != no ] || a+=(--defaultno)
    dlg "${a[@]}" --yesno "$1" "$(height 10)" "$COLS" && return 0
    status=$?
    [ "$status" -eq 1 ] && return 1
    abort
}

radio()
{
    local title=$1
    shift
    dlg --radiolist "$title" "$(height 16)" "$COLS" 8 "$@"
}

checklist()
{
    local title=$1
    shift
    dlg --separate-output --checklist "$title" "$(height 18)" "$COLS" 10 "$@"
}

say()
{
    dlg --msgbox "$1" "$(height "$2")" "$COLS" || true
}

bad()
{
    say "Invalid input:\n\n$1" 9
    return 1
}

quote()
{
    printf "'%s'" "${1//\'/\'\\\'\'}"
}

json_list()
{
    python3 -c 'import json,sys ; print(json.dumps([x.strip() for x in sys.argv[1].split(",") if x.strip()],separators=(",",":")))' "$1"
}

require_nonempty()
{
    local value=$1 message=$2
    [ -n "$value" ] || bad "$message" || abort
}

tristate()
{
    local title=$1 current=${2:-} picked
    local auto=OFF force_true=OFF force_false=OFF
    case "$current" in
        true) force_true=ON ;;
        false) force_false=ON ;;
        *) auto=ON ;;
    esac
    picked=$(radio "$title" \
        auto "Use the vault's default policy" "$auto" \
        true "Force true" "$force_true" \
        false "Force false" "$force_false") || return 1
    [ "$picked" != auto ] && printf %s "$picked" || printf %s ""
}

choose_vm()
{
    local platform=$1 label=$2 name picked
    local choices=()
    while IFS= read -r name ; do
        [ -z "$name" ] || choices+=("${name%%$'\t'*}" "${name#*$'\t'}" OFF)
    done < <(
        python3 -c 'import json,sys,datetime,os
def when(v):
    try:
        d=datetime.datetime.fromisoformat(v.replace("Z","+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=datetime.timezone.utc)
    except Exception:
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
try: rows=json.load(open(os.path.join(os.environ["MIGRATE_V2V_CATALOG_DIR"],"migration-catalog.json"))).get("migrations",[])
except Exception: rows=[]
best={}
for row in rows:
    name=row.get("vm_name")
    stamp=row.get("migrated_at") or ""
    if not name or row.get("target_platform")!=sys.argv[1] or row.get("status")!="completed":
        continue
    if name not in best or when(stamp)>when(best[name]): best[name]=stamp
for name,stamp in sorted(best.items(), key=lambda item: when(item[1]), reverse=True):
    print("%s\t%s" % (name, stamp[:10] or "undated"))' "$platform"
    )
    if [ ${#choices[@]} -eq 0 ] ; then
        say "No completed ${label} migrations in $MIGRATE_V2V_CATALOG_DIR/migration-catalog.json.\\n\\nType the VM name manually instead." 10 || true
        input "VM name (as recorded in the migration catalog):" ""
        return
    fi
    choices[2]=ON
    choices+=("_other" "Type a different VM name" OFF)
    picked=$(dlg --radiolist "Which VM do you want to launch?\n(Newest completed ${label} migration first.)" "$(height 16)" "$COLS" 8 "${choices[@]}") || return 1
    [ "$picked" != _other ] && printf %s "$picked" || input "VM name:" ""
}

choose_vm_required()
{
    local platform=$1 label=$2 vm
    while : ; do
        vm=$(choose_vm "$platform" "$label") || return 1
        if [ -n "$vm" ] ; then
            printf %s "$vm"
            return 0
        fi
        bad "The VM name cannot be empty." || true
    done
}

vault_keys()
{
    local vault=$1 map_name=$2
    # Same order as edit-vault.sh: VAULT_PASSWORD_FILE if it names a non-empty
    # file, else ~/.dib-vault-pass, else the old ~/.ssh/dib-vault-pass.
    local passfile="${VAULT_PASSWORD_FILE:-}"
    [ -n "$passfile" ] && [ -s "$passfile" ] || passfile="$HOME/.dib-vault-pass"
    [ -s "$passfile" ] || passfile="$HOME/.ssh/dib-vault-pass"
    [ -s "$passfile" ] || return 1
    local vault_bin=ansible-vault
    local venv_bin="${DIB7_VENV_BIN:-$HOME/.dib7/bin}"
    [ -x "$venv_bin/ansible-vault" ] && vault_bin="$venv_bin/ansible-vault"
    command -v "$vault_bin" > /dev/null 2>&1 || return 1
    "$vault_bin" view "$vault" --vault-password-file "$passfile" 2> /dev/null < /dev/null |
        python3 -c 'import sys
try:
    import yaml
    data = yaml.safe_load(sys.stdin) or {}
except Exception:
    raise SystemExit(1)
for key in (data.get(sys.argv[1]) or {}):
    print(key)' "$map_name" 2> /dev/null
}

choose_target()
{
    local title=$1 vault=$2 map_name=$3 current=$4 picked
    local choices=()
    local matched=false
    while IFS= read -r name ; do
        if [ -n "$name" ] ; then
            if [ "$name" = "$current" ] ; then
                choices+=("$name" "in ${vault}" ON)
                matched=true
            else
                choices+=("$name" "in ${vault}" OFF)
            fi
        fi
    done < <(vault_keys "$vault" "$map_name" || true)
    if [ ${#choices[@]} -eq 0 ] ; then
        input "$title (blank lets the playbook auto-select):" "$current"
        return
    fi
    [ "$matched" = true ] || [ -n "$current" ] || choices[2]=ON
    choices+=("_auto" "Let the playbook pick a single-entry vault" OFF)
    if [ -n "$current" ] && [ "$matched" = false ] ; then
        choices+=("_other" "Type a different name" ON)
    else
        choices+=("_other" "Type a different name" OFF)
    fi
    picked=$(dlg --radiolist "$title" "$(height 16)" "$COLS" 8 "${choices[@]}") || return 1
    case "$picked" in
        _auto) printf %s "" ;;
        _other) input "$title:" "$current" ;;
        *) printf %s "$picked" ;;
    esac
}

finish()
{
    local file=$1 vm=$2 verb=${3:-launch} saved=""
    if yesno "Review the generated command?\n\nNothing runs yet." yes ; then
        dlg --textbox "$file" "$(height 22)" "$COLS" || true
    fi
    if yesno "Save the command to a file?" no ; then
        while : ; do
            saved=$(input "Save where:" "./${verb}-${vm}.sh") || abort
            if [ -z "$saved" ] ; then
                bad "Choose a file path." || true
                continue
            fi
            if [ -e "$saved" ] || [ -L "$saved" ] ; then
                bad "The file already exists: $saved" || true
                continue
            fi
            if (set -C; cat -- "$file" > "$saved") ; then
                saved=$(realpath -- "$saved")
                chmod +x "$saved"
                rm -f -- "$file"
                file=$saved
                say "Saved to $saved." 8
                break
            fi
            bad "Could not write $saved." || true
        done
    fi
    if yesno "Run the ${verb} now?" no ; then
        bash "$file"
    else
        say "The command is in $file.\n\nRun it with:\n  bash $file" 10
    fi
}
