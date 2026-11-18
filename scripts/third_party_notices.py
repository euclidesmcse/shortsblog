"""Gera o THIRD-PARTY-NOTICES.md de uma imagem (E19.6, D-112): pacotes Python instalados e licenças.

Roda DENTRO do container da imagem (lê os metadados do pip), só leitura:

    docker exec -i <container> python - "<título>" < scripts/third_party_notices.py \
        > apps/<app>/licenses/THIRD-PARTY-NOTICES.md

Regenerar quando mudar o requirements.txt (o CI do pacote não confere isto sozinho).
"""
import importlib.metadata as metadata
import sys

title = sys.argv[1] if len(sys.argv) > 1 else "imagem"


def license_of(dist) -> str:
    md = dist.metadata
    expression = md.get("License-Expression")
    if expression:
        return expression
    classifiers = [c.split("::")[-1].strip() for c in (md.get_all("Classifier") or [])
                   if c.startswith("License ::")]
    if classifiers:
        return "; ".join(classifiers)
    return (md.get("License") or "não declarada").split("\n")[0][:80]


rows = sorted({(d.metadata["Name"], d.version, license_of(d)) for d in metadata.distributions()},
              key=lambda r: r[0].lower())
print(f"# Componentes de terceiros — {title}\n")
print("Pacotes Python instalados na imagem, com a licença declarada por cada um (metadados do pip).")
print("Os textos das licenças acompanham cada pacote em `site-packages/<pacote>.dist-info/`.\n")
print("Observações (revisão de procedência da E19.6, `docs/reviews/E19.6-procedencia.md`):\n")
print("- `psycopg`/`psycopg-binary` (LGPL-3.0): usados como biblioteca, sem modificação; podem ser")
print("  trocados por outra versão com `pip install`.")
print("- `certifi` (MPL-2.0): sem modificação.")
print("- `text-unidecode` (Artistic ou GPL): usado sob a Artistic License.\n")
print("| Pacote | Versão | Licença |\n|---|---|---|")
for name, version, lic in rows:
    print(f"| {name} | {version} | {lic.replace('|', '/')} |")
