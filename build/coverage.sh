#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf '%s\n' 'usage: coverage.sh --repo-root <absolute-path> --python-data <absolute-path> --python-report <absolute-path> --dotnet-project <id> <absolute-csproj> <absolute-output-prefix> <absolute-include-or-dash> [--dotnet-project ...]...' >&2
  exit 64
}

is_absolute_path() {
  [[ "$1" == /* || "$1" =~ ^[A-Za-z]:[\\/].* ]]
}

to_shell_path() {
  printf '%s\n' "${1//\\//}"
}

repo_root=""
python_data=""
python_report=""
declare -a dotnet_ids=()
declare -a dotnet_projects=()
declare -a dotnet_output_prefixes=()
declare -a dotnet_include_directories=()

while (( $# > 0 )); do
  case "$1" in
    --repo-root)
      (( $# >= 2 )) || usage
      [[ -z "$repo_root" && -n "$2" ]] || usage
      repo_root="$2"
      shift 2
      ;;
    --python-data)
      (( $# >= 2 )) || usage
      [[ -z "$python_data" && -n "$2" ]] || usage
      python_data="$2"
      shift 2
      ;;
    --python-report)
      (( $# >= 2 )) || usage
      [[ -z "$python_report" && -n "$2" ]] || usage
      python_report="$2"
      shift 2
      ;;
    --dotnet-project)
      (( $# >= 5 )) || usage
      [[ -n "$2" && -n "$3" && -n "$4" && -n "$5" ]] || usage
      dotnet_ids+=("$2")
      dotnet_projects+=("$3")
      dotnet_output_prefixes+=("$4")
      dotnet_include_directories+=("$5")
      shift 5
      ;;
    *)
      usage
      ;;
  esac
done

[[ -n "$repo_root" && -n "$python_data" && -n "$python_report" ]] || usage
is_absolute_path "$repo_root" || usage
is_absolute_path "$python_data" || usage
is_absolute_path "$python_report" || usage
(( ${#dotnet_ids[@]} == 5 )) || usage

shell_repo_root="$(to_shell_path "$repo_root")"
shell_python_data="$(to_shell_path "$python_data")"
shell_python_report="$(to_shell_path "$python_report")"

expected_ids=(codesearch-core host stateless-preview stateless host-prompts)
for index in "${!expected_ids[@]}"; do
  [[ "${dotnet_ids[$index]}" == "${expected_ids[$index]}" ]] || usage
  is_absolute_path "${dotnet_projects[$index]}" || usage
  is_absolute_path "${dotnet_output_prefixes[$index]}" || usage
  if [[ "${dotnet_ids[$index]}" == "stateless" ]]; then
    [[ "${dotnet_include_directories[$index]}" != "-" ]] || usage
    is_absolute_path "${dotnet_include_directories[$index]}" || usage
  else
    [[ "${dotnet_include_directories[$index]}" == "-" ]] || usage
  fi
done

cd "$shell_repo_root"
mkdir -p "$(dirname "$shell_python_data")" "$(dirname "$shell_python_report")"
python_cache_directory="$(dirname "$shell_python_data")/.pytest_cache"
python_test_paths=(
  tests/test_app_type.py
  tests/test_backends.py
  tests/test_build_cleanup.py
  tests/test_build_manager.py
  tests/test_build_policy.py
  tests/test_build_session.py
  tests/test_build_state.py
  tests/test_client.py
  tests/test_code_search.py
  tests/test_collection_analyzer.py
  tests/test_context_tools.py
  tests/test_debug_freshness.py
  tests/test_debug_launch_preflight.py
  tests/test_host_proxy.py
  tests/test_inspection_tools.py
  tests/test_process_registry.py
  tests/test_project_utils.py
  tests/test_protocol.py
  tests/test_resource_updates.py
  tests/test_runtime_smoke_runner.py
  tests/test_runtime_smoke_schema.py
  tests/test_runtime_smoke_v2_actions.py
  tests/test_runtime_smoke_v2_cleanup.py
  tests/test_session.py
  tests/test_source_context.py
  tests/test_state.py
  tests/test_stealth_mode.py
  tests/test_ui_backend.py
  tests/test_ui_evidence.py
  tests/test_ui_grid_helpers.py
  tests/test_ui_new_tools.py
  tests/test_ui_screenshot.py
)
coverage run --source=src/netcoredbg_mcp --data-file="$shell_python_data" -m pytest \
  --cache-clear -o "cache_dir=$python_cache_directory" "${python_test_paths[@]}"
coverage xml --data-file="$shell_python_data" -o "$shell_python_report"

coverage_root="$(dirname "$(dirname "$shell_python_data")")"
preview_publish_directory="$coverage_root/preview/publish"
preview_artifact_directory="$coverage_root/preview/artifact"
preview_project="$shell_repo_root/host/NetCoreDbg.Mcp.Stateless.Preview/NetCoreDbg.Mcp.Stateless.Preview.csproj"
source_commit="$(git -C "$shell_repo_root" rev-parse HEAD)"
dotnet publish "$preview_project" \
  --configuration Release \
  --runtime win-x64 \
  --self-contained true \
  -nr:false \
  -p:PublishSingleFile=true \
  -p:PublishTrimmed=false \
  --output "$preview_publish_directory"
cp "$preview_publish_directory/NetCoreDbg.Mcp.Stateless.Preview.exe" \
  "$preview_publish_directory/netcoredbg-mcp-stateless-preview.exe"
python "$shell_repo_root/build/prepare_preview_fixture.py" \
  --executable "$preview_publish_directory/netcoredbg-mcp-stateless-preview.exe" \
  --output "$preview_artifact_directory" \
  --commit "$source_commit"

for index in "${!dotnet_ids[@]}"; do
  project="$(to_shell_path "${dotnet_projects[$index]}")"
  output_prefix="$(to_shell_path "${dotnet_output_prefixes[$index]}")"
  include_directory="${dotnet_include_directories[$index]}"
  if [[ "$include_directory" != "-" ]]; then
    include_directory="$(to_shell_path "$include_directory")"
  fi
  mkdir -p "$(dirname "$output_prefix")"
  dotnet restore "$project" -nr:false

  test_arguments=(
    "$project"
    --configuration Debug
    --no-restore
    -nr:false
    -p:CollectCoverage=true
    -p:CoverletOutputFormat=cobertura
    "-p:CoverletOutput=$output_prefix"
  )
  if [[ "$include_directory" != "-" ]]; then
    test_arguments+=("-p:IncludeDirectory=$include_directory")
  fi
  if [[ "${dotnet_ids[$index]}" == "stateless" ]]; then
    test_arguments+=(--filter "Coverage!=Exclude")
  fi
  if [[ "${dotnet_ids[$index]}" == "stateless-preview" ]]; then
    NETCOREDBG_PREVIEW_ARTIFACT_ROOT="$preview_artifact_directory" dotnet test "${test_arguments[@]}"
  else
    dotnet test "${test_arguments[@]}"
  fi
done
