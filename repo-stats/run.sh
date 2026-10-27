#!/bin/sh
# repo-stats with a read-only API token kept in the OS keychain.
#
#   run.sh --store github|gitlab [--hostname HOST] [--project GROUP/PROJECT] [--owner OWNER]
#       Get a read-only token and save it in the keychain. On GitHub this opens the token page with
#       read-only permissions filled in; you paste the token at a hidden prompt. On GitLab with --project,
#       it first tries `glab token create` (read_api, 30 days), then falls back to the token page.
#   run.sh --forget github|gitlab [--hostname HOST]
#       Delete the saved token.
#   run.sh [repo_stats.py options]
#       Run repo-stats. A saved token is given to gh/glab for this run only, as GH_TOKEN,
#       GH_ENTERPRISE_TOKEN or GITLAB_TOKEN. Without one, gh/glab use their own login.
#
# The token is never echoed, never passed as a command argument (where `ps` could see it) and never
# written to shell history or a file. repo_stats.py itself never reads it: only gh/glab do, and
# repo_stats.py removes it from the environment of the git commands it runs.
#
# Keychains: macOS Keychain (`security`) or the Linux Secret Service (`secret-tool`, from libsecret-tools).
set -eu

here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
service=repo-stats
tok=

die() { printf 'run.sh: %s\n' "$*" >&2; exit 1; }
note() { printf '%s\n' "$*" >&2; }

keychain() {
  if [ "$(uname -s)" = Darwin ] && command -v security >/dev/null 2>&1; then echo macos
  elif command -v secret-tool >/dev/null 2>&1; then echo secret-tool
  else echo none
  fi
}

check_host() {
  case $1 in
    ''|-*|*[!A-Za-z0-9.-]*) die "invalid host name '$1'" ;;
  esac
}

lookup() {  # print the saved token for host $1, or nothing
  case $(keychain) in
    macos) security find-generic-password -a "$1" -s "$service" -w 2>/dev/null || true ;;
    secret-tool) secret-tool lookup service "$service" host "$1" 2>/dev/null || true ;;
  esac
}

save() {  # save $tok for host $1
  [ -n "$tok" ] || die "no token given"
  case $tok in
    *[!A-Za-z0-9_.-]*) tok=; die "that does not look like a token (unexpected characters); nothing saved" ;;
  esac
  case $(keychain) in
    macos)
      # `security -i` reads the command from stdin, so the token is not in any process's arguments.
      printf 'add-generic-password -U -a "%s" -s "%s" -l "%s" -w "%s"\n' "$1" "$service" "repo-stats $1" "$tok" \
        | security -i >/dev/null ;;
    secret-tool)
      printf '%s' "$tok" | secret-tool store --label="repo-stats $1 (read-only)" service "$service" host "$1" ;;
    *) tok=; die "no supported keychain found. Install secret-tool (libsecret-tools) or use 'gh auth login' / 'glab auth login'." ;;
  esac
  tok=
}

forget() {
  case $(keychain) in
    macos) security delete-generic-password -a "$1" -s "$service" >/dev/null 2>&1 || true ;;
    secret-tool) secret-tool clear service "$service" host "$1" 2>/dev/null || true ;;
  esac
}

read_secret() {  # read a line into $tok without echoing it
  if [ -t 0 ]; then
    printf '%s' "$1" >&2
    trap 'stty echo 2>/dev/null' EXIT INT TERM
    stty -echo
    IFS= read -r tok || true
    stty echo
    trap - EXIT INT TERM
    printf '\n' >&2
  else
    IFS= read -r tok || true
  fi
}

open_url() {  # open $1 in a browser when run from a terminal; always print it
  note "  $1"
  [ -t 1 ] || return 0
  case $(uname -s) in
    Darwin) open "$1" 2>/dev/null || true ;;
    *) if command -v xdg-open >/dev/null 2>&1; then xdg-open "$1" >/dev/null 2>&1 || true; fi ;;
  esac
}

remote_owner() {  # best effort: the owner/group of origin when it is on host $1
  url=$(git remote get-url origin 2>/dev/null) || return 0
  printf '%s\n' "$url" | sed -n -E "s#^([a-z+]+://)?([^@/]+@)?$1[:/]([^/]+)/.*#\\3#p" | head -n 1
}

store() {
  provider=$1; shift
  host='' project='' owner=''
  while [ $# -gt 0 ]; do
    case $1 in
      --hostname) host=${2:-}; shift 2 ;;
      --hostname=*) host=${1#*=}; shift ;;
      --project) project=${2:-}; shift 2 ;;
      --project=*) project=${1#*=}; shift ;;
      --owner) owner=${2:-}; shift 2 ;;
      --owner=*) owner=${1#*=}; shift ;;
      *) die "unknown option for --store: $1" ;;
    esac
  done
  case $provider in
    github) host=${host:-github.com} ;;
    gitlab) host=${host:-gitlab.com} ;;
    *) die "--store needs github or gitlab" ;;
  esac
  check_host "$host"
  [ "$(keychain)" != none ] || die "no supported keychain found. Install secret-tool (libsecret-tools) or use 'gh auth login' / 'glab auth login'."

  if [ "$provider" = gitlab ] && [ -n "$project" ] && command -v glab >/dev/null 2>&1; then
    note "Creating a read-only project access token for $project with glab (read_api, Reporter, 30 days) ..."
    tok=$(cd "${TMPDIR:-/tmp}" && GLAB_SEND_TELEMETRY=false GLAB_NO_PROMPT=1 GLAB_CHECK_UPDATE=false \
          glab token create --repo "https://$host/$project" --access-level reporter --scope read_api \
          --duration 30d --description "Read-only token for repo-stats" "repo-stats-$(date +%Y%m%d%H%M%S)") || tok=
    if [ -n "$tok" ]; then
      save "$host"
      note "Saved. Run: $0 /path/to/repo"
      return 0
    fi
    note "glab could not create a project access token (on GitLab.com they need Premium or Ultimate and the"
    note "Maintainer role). Create a personal token instead:"
  fi

  if [ "$provider" = github ]; then
    owner=${owner:-$(remote_owner "$host")}
    url="https://$host/settings/personal-access-tokens/new?name=repo-stats+read-only&description=Read-only+token+for+repo-stats&expires_in=30&contents=read&pull_requests=read"
    [ -z "$owner" ] || url="$url&target_name=$owner"
    note "1. Open this page (it fills in a read-only token: Contents and Pull requests, read; 30 days):"
    open_url "$url"
    note "2. Under Repository access, choose 'Only select repositories' and pick the repositories to analyse."
    note "3. Click 'Generate token' and copy it."
  else
    note "1. Open this page (it fills in a token with only the read_api scope):"
    open_url "https://$host/-/user_settings/personal_access_tokens?name=repo-stats+read-only&scopes=read_api"
    note "2. Set an expiry date, click 'Create token' and copy it."
  fi
  read_secret "Paste the token (it will not be shown): "
  save "$host"
  note "Saved in the keychain for $host. Run: $0 /path/to/repo"
}

case ${1:-} in
  --store)
    shift
    [ $# -gt 0 ] || die "--store needs github or gitlab"
    store "$@"
    exit 0 ;;
  --forget)
    shift
    case ${1:-} in
      github) host=github.com ;;
      gitlab) host=gitlab.com ;;
      *) die "--forget needs github or gitlab" ;;
    esac
    shift
    case ${1:-} in
      --hostname) host=${2:-} ;;
      --hostname=*) host=${1#*=} ;;
    esac
    check_host "$host"
    forget "$host"
    note "Removed the saved token for $host (if there was one)."
    exit 0 ;;
esac

# Run repo-stats. Find --hostname/--provider among its options to pick the saved token, if any.
hostname='' provider='' prev=''
for a in "$@"; do
  case $prev in
    --hostname) hostname=$a ;;
    --provider) provider=$a ;;
  esac
  case $a in
    --hostname=*) hostname=${a#*=} ;;
    --provider=*) provider=${a#*=} ;;
  esac
  prev=$a
done
hostname=$(printf '%s' "$hostname" | tr '[:upper:]' '[:lower:]')

if [ "$(keychain)" != none ]; then
  if [ -n "$hostname" ]; then
    check_host "$hostname"
    case $hostname in
      github.com) provider=${provider:-github} ;;
      gitlab.com) provider=${provider:-gitlab} ;;
    esac
    t=$(lookup "$hostname")
    if [ -n "$t" ]; then
      case $provider in
        github)
          # gh sends GH_TOKEN only to github.com and GH_ENTERPRISE_TOKEN to other hosts; repo-stats passes
          # this same host to gh as --hostname.
          if [ "$hostname" = github.com ]; then GH_TOKEN=$t; export GH_TOKEN
          else GH_ENTERPRISE_TOKEN=$t; export GH_ENTERPRISE_TOKEN; fi
          note "Using the read-only token for $hostname from the keychain." ;;
        gitlab)
          GITLAB_TOKEN=$t; export GITLAB_TOKEN
          note "Using the read-only token for $hostname from the keychain." ;;
      esac
    fi
  else
    t=$(lookup github.com)
    if [ -n "$t" ]; then GH_TOKEN=$t; export GH_TOKEN; note "Using the read-only github.com token from the keychain."; fi
    t=$(lookup gitlab.com)
    if [ -n "$t" ]; then GITLAB_TOKEN=$t; export GITLAB_TOKEN; note "Using the read-only gitlab.com token from the keychain."; fi
  fi
  t=
fi

exec "${PYTHON:-python3}" "$here/repo_stats.py" "$@"
