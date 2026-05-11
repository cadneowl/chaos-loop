#!/usr/bin/env bash
# Print the catalogue.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "${REPO_ROOT}"
source .venv/bin/activate

python -c "
from agents.chaos.faults._meta import CATALOGUE
print(f'{\"name\":<24}{\"category\":<12}{\"approval\":<10}{\"chaos_mesh_kind\":<18}description')
print('-' * 100)
for name, f in sorted(CATALOGUE.items()):
    print(f'{name:<24}{f.category.value:<12}{str(f.requires_approval):<10}{(f.chaos_mesh_kind or \"custom\"):<18}{f.description}')
"
