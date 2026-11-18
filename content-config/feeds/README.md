# Feeds RSS dos blogs (E5)

`<site>.yaml` tem os feeds **validados** de cada blog; `candidatos/<site>.yaml` tem todos os que foram
tentados. Os ficheiros finais são gerados por `scripts/validate_feeds.py` — não se editam à mão (o
`--check` falha se um feed final não vier dos candidatos).

## Regra de uso (D-013)

O RSS serve para **descobrir** notícias e **citar** a fonte. Nunca se copia o texto nem as imagens de um
feed: o artigo é escrito de raiz, com atribuição e ligação à fonte na secção "Fontes e créditos". Crédito
não é licença de republicação. Do feed guarda-se só título, ligação e data.

## Critérios de validação

O `robots.txt` do sítio não pode proibir o nosso robô (a mesma regra do motor); HTTPS (redirecionamentos
só para HTTPS), HTTP 200, RSS/Atom bem formado, item mais recente com link HTTPS e
data com menos de 30 dias (datas no futuro não contam), e a maioria dos itens com data e link. O validador
identifica-se como `ShortsBlogBot`, faz um pedido de cada vez por servidor com 2 s de intervalo e respeita
o `Retry-After`. **Um sítio que bloqueia o nosso robô (HTTP 403) fica de fora**: não se contorna o bloqueio
com um identificador falso, porque o motor em produção seria bloqueado da mesma maneira.

Três resultados possíveis: **válido**, **inválido** e **não validado** (falha passageira — 429, 5xx, tempo
esgotado — depois de três tentativas: não se sabe se é válido; não entra nesta corrida).

```bash
scripts/validate_feeds.py            # valida candidatos/, regenera <site>.yaml e docs/reviews/feeds-report.md
scripts/validate_feeds.py --check    # valida os finais; falha com inválidos, blog < 15 ou feed fora dos candidatos
python3 -m unittest discover -s scripts/tests
```

O modo de escrita recusa regravar um blog que ficasse com menos de 15 feeds ou perdesse mais de 30% (rede
em baixo, bloqueios em série); `--force` ultrapassa essa salvaguarda.

**Atenção (2026-10-09):** `validate_feeds.py` **sem `--check` regrava todos os `<site>.yaml` e o
`docs/reviews/feeds-report.md`**. Uma execução assim, de origem desconhecida, trocou feeds nos arquivos
finais: tirou dois da Câmara por falha passageira (HTTP 429) e pôs outros que ainda não tinham os termos
de uso revisados (D-065, E14.5j). Feed NOVO nos arquivos finais entra no banco no próximo deploy; feed
alterado ou retirado só muda o banco com a reimportação (`make reimport-apply`, E18.3a). Por isso: antes de rodar, confira `git status`; depois de rodar, leia `git diff` e só
commite o que foi decidido; para testar candidatos novos sem gravar, importe `validate_feeds` e chame
`validate_with_retry` num script à parte (foi assim a triagem em `docs/reviews/E14-triagem-awesome-rss-2026-10-09.md`).

## Estado em 2026-10-04 (meta: ≥ 15 por blog)

289 candidatos, 163 válidos.

| Blog | Válidos | oficial | agência | veículo | fornecedor | académico |
|---|---:|---:|---:|---:|---:|---:|
| cloud | 29 | 3 | 0 | 7 | 19 | 0 |
| ia | 24 | 1 | 0 | 9 | 12 | 2 |
| financas | 22 | 8 | 2 | 12 | 0 | 0 |
| saude | 32 | 10 | 1 | 15 | 0 | 6 |
| politica | 32 | 11 | 3 | 18 | 0 | 0 |
| culinaria | 24 | 1 | 1 | 19 | 1 | 2 |

`tier`: `oficial` (órgãos públicos e organismos internacionais), `agencia` (agências de notícias),
`veiculo` (imprensa e publicações), `fornecedor` (blogs oficiais de empresas), `academico` (revistas
científicas e universidades).

## Equilíbrio e confiabilidade

- **Política.** Três grupos de veículos:
  - generalistas de referência, sem nota: Folha, Estadão, G1, Poder360, Metrópoles, JOTA, Conjur, Nexo,
    BBC Brasil, RFI;
  - linha editorial marcada, com nota — de um lado Gazeta do Povo, O Antagonista e Veja; do outro
    CartaCapital, The Intercept Brasil e Agência Pública (três de cada lado);
  - duas agências de checagem (Aos Fatos, Lupa), com nota própria.

  As fontes de linha marcada servem para descobrir temas; os factos confirmam-se em fonte oficial ou
  agência. O **Planalto** é comunicação do governo em funções e a **Agência Brasil** é uma agência pública:
  ambos levam nota. Com os tribunais e os órgãos de controlo fora (ver lacunas), o `oficial` de Política
  é só Executivo, Legislativo e organismos internacionais.
- **Saúde.** Organismos oficiais (OPAS, CDC, EMA, ECDC, ONU, Fiocruz), revistas científicas e imprensa.
  Notas: as autoridades estrangeiras (CDC, EMA, ECDC) não valem para o Brasil e a jurisdição diz-se
  sempre; os agregadores de comunicados (Medical Xpress, ScienceDaily) servem para chegar ao estudo
  original, não para corroborar; os feeds científicos citam-se sem extrapolar conclusões clínicas; os de
  bem-estar (Drauzio Varella, Veja Saúde, Metrópoles) não se transformam em conselho clínico.
- **Fornecedores (cloud e IA):** são a fonte primária de um anúncio, não uma avaliação independente.
- **Feeds generalistas** (Agência Brasil — Geral, BBC Brasil, Senado, Exame, NIST, Financial Times — Mundo…)
  trazem temas fora do blog: o filtro temático é da E6.
- **O mesmo conteúdo em vários feeds e blogs** (ONU e OPAS em duas línguas, Senado e BBC Brasil em três
  blogs): a deduplicação é da E6.1.

## Lacunas conhecidas

Fontes que se queriam e não passaram a validação nesta data (detalhe em `docs/reviews/feeds-report.md`):

- **Saúde, oficiais do Brasil:** Ministério da Saúde e ANS (o endereço `…/RSS` do gov.br devolve XML
  inválido; no caso do Ministério verificou-se que redireciona para uma página "Unauthorized": o feed
  deixou de ser público), Anvisa e INCA (404), Butantan (403), Fiocruz portal (parado há mais de um ano; a
  Agência Fiocruz passa). A OMS em inglês está parada há meses; entram a OPAS e a ONU. NIH, NEJM e The
  Lancet bloqueiam o robô. A FDA e o BMJ Global Health publicam itens com ligações `http://`, que o motor
  não vai buscar.
- **Política, oficiais:** STF (último item com mais de 30 dias), TSE (403), STJ (itens sem data), TCU, CGU,
  CNJ, MPF, AGU, MRE e Ministério da Justiça (XML inválido ou 404; nos do gov.br a causa provável é a
  mesma do Ministério da Saúde, mas só esse foi verificado).
- **Finanças, oficiais:** CVM e Receita Federal (parados há mais de 30 dias), IBGE e FMI (403), Ministério
  da Fazenda e Tesouro Nacional (404). Entram o Banco Central, Câmara, Senado, Fed, BCE, BIS e SEC.
- **Culinária:** só um oficial (Câmara — Agropecuária, que é política agrícola e leva nota de
  generalista); Embrapa e USDA bloqueiam o robô, FAO e Panelinha deram 404, TudoGostoso e Receiteria
  bloqueiam.
- **IA:** a Anthropic não publica RSS; a FAPESP devolveu um feed sem itens; os três feeds do **arXiv**
  (cs.AI, cs.CL, cs.LG) vieram vazios porque a validação correu num sábado e o arXiv não publica ao fim de
  semana — voltar a correr em dia útil.
- **Proibidos pelo `robots.txt`** (verificação acrescentada em 2026-10-04, depois de o motor ter recusado o
  Canaltech na primeira descoberta real): Canaltech, Oracle Cloud Infrastructure Blog, DevOps.com, SciELO em
  Perspectiva, El Comidista, entre outros que já falhavam por 403.
- **Não validados nesta corrida** (falha passageira): VentureBeat AI e Food52 (429), IPEA (500), Jornal
  da USP (TLS sem resposta).

**Consequência para a E6:** como as fontes oficiais brasileiras não têm feed utilizável, a corroboração
"em fonte oficial" que as notas pedem tem de ir buscar a página oficial diretamente (E6.3).

Rever esta lista periodicamente: uma corrida do validador noutro dia recupera alguns.
