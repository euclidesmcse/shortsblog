<?php
/**
 * Plugin Name: ShortsBlog — Receitas
 * Description: Formato de portal de receitas na Culinária (D-092, spec `docs/spec/receitas-formato.md`),
 *              em duas camadas: (1) decoração do HTML na renderização — cartão de informações,
 *              ingredientes com checkbox nativo (sem JavaScript, D-046) e passos numerados em bloco;
 *              (2) JSON-LD `schema.org/Recipe` no lugar do Article (filtro `sb_seo_jsonld` do sb-seo.php).
 *              Só age quando SB_SITE_KEY = culinaria E o post traz os títulos-âncora que o motor gera
 *              (`article.py`, modo receita); sem âncora, nada muda (falha segura). Nada aqui altera o
 *              que está guardado no banco: o post continua o HTML puro do motor.
 */

if (!defined('ABSPATH')) {
    exit;
}

/** Só o blog de culinária tem modo receita. */
function sb_recipe_enabled(): bool
{
    return defined('SB_SITE_KEY') && SB_SITE_KEY === 'culinaria';
}

/**
 * Papel de um <h2> do motor. Os títulos são GARANTIDOS pela tabela canônica
 * `SECTIONS_BY_LANG` do motor (`apps/engine/app/services/recipe.py`): depois de traduzir,
 * `localize_structure` reescreve os <h2> por posição — este mapa é um espelho dela, e
 * mudar lá exige mudar aqui. Devolve 'info', 'ingredients', 'steps' ou null.
 */
function sb_recipe_role(string $heading): ?string
{
    $map = [
        'info' => ['informações da receita', 'recipe information', 'información de la receta', '食谱信息'],
        'ingredients' => ['ingredientes', 'ingredients', '食材'],
        'steps' => ['modo de preparo', 'instructions', 'modo de preparación', '制作方法'],
    ];
    $text = mb_strtolower(trim(wp_strip_all_tags($heading)));
    foreach ($map as $role => $titles) {
        if (in_array($text, $titles, true)) {
            return $role;
        }
    }
    return null;
}

/**
 * Fatia o HTML do post nos <h2>: [['role' => ?string, 'heading' => string, 'body' => string], …].
 * A fatia 0 é o preâmbulo (role null, heading ''). Reagrupar heading+body devolve o original.
 */
function sb_recipe_sections(string $html): array
{
    $parts = preg_split('/(<h2[^>]*>.*?<\/h2>)/su', $html, -1, PREG_SPLIT_DELIM_CAPTURE);
    if ($parts === false) {
        return [['role' => null, 'heading' => '', 'body' => $html]];
    }
    $sections = [['role' => null, 'heading' => '', 'body' => array_shift($parts) ?? '']];
    for ($i = 0; $i < count($parts); $i += 2) {
        $heading = $parts[$i];
        $sections[] = ['role' => sb_recipe_role($heading), 'heading' => $heading, 'body' => $parts[$i + 1] ?? ''];
    }
    return $sections;
}

/** Rótulo e valor de um item «Rótulo: valor» (aceita os dois-pontos largos do chinês). */
function sb_recipe_label_value(string $item): ?array
{
    $split = preg_split('/[:：]/u', wp_strip_all_tags($item), 2);
    if ($split === false || count($split) < 2 || trim($split[1]) === '') {
        return null;
    }
    return [trim($split[0]), trim($split[1])];
}

// ───── Camada 1: decoração na renderização (sem JavaScript) ─────────────────────────────

/** A lista de «Informações da receita» vira um cartão de menu (rótulo pequeno, valor em serifa). */
function sb_recipe_info_card(string $body): string
{
    return preg_replace_callback('/<ul>(.*?)<\/ul>/su', static function (array $m): string {
        $items = [];
        if (preg_match_all('/<li>(.*?)<\/li>/su', $m[1], $lis)) {
            foreach ($lis[1] as $li) {
                $pair = sb_recipe_label_value($li);
                $items[] = $pair === null
                    ? '<div class="sb-r-item"><span class="sb-r-value">' . $li . '</span></div>'
                    : '<div class="sb-r-item"><span class="sb-r-label">' . esc_html($pair[0])
                        . '</span><span class="sb-r-value">' . esc_html($pair[1]) . '</span></div>';
            }
        }
        return $items === [] ? $m[0] : '<div class="sb-r-info">' . implode('', $items) . '</div>';
    }, $body, 1) ?? $body;
}

/** Cada ingrediente ganha um checkbox nativo (clicável sem JS; impresso vira lista de compras). */
function sb_recipe_checkboxes(string $body): string
{
    $body = preg_replace_callback('/<li>(.*?)<\/li>/su', static fn(array $m): string =>
        '<li class="sb-r-ing"><label><input type="checkbox"><span>' . $m[1] . '</span></label></li>', $body) ?? $body;
    return str_replace('<ul>', '<ul class="sb-r-ings">', $body);
}

add_filter('the_content', static function (string $content): string {
    if (!sb_recipe_enabled() || is_feed() || !is_singular('post')) {
        return $content;
    }
    $sections = sb_recipe_sections($content);
    if (count($sections) < 2) {
        return $content;
    }
    $out = '';
    foreach ($sections as $s) {
        if ($s['role'] === 'info') {
            $s['body'] = sb_recipe_info_card($s['body']);
        } elseif ($s['role'] === 'ingredients') {
            $s['body'] = sb_recipe_checkboxes($s['body']);
        } elseif ($s['role'] === 'steps') {
            $s['body'] = preg_replace('/<ol>/u', '<ol class="sb-r-steps">', $s['body'], 1) ?? $s['body'];
        }
        $out .= $s['heading'] . $s['body'];
    }
    return $out;
}, 20);

// ───── Camada 2: JSON-LD schema.org/Recipe no lugar do Article ──────────────────────────

/** «1 hora e 13 minutos (…)» → PT1H13M, nos 4 idiomas; null quando não dá para ler (omite-se o campo). */
function sb_recipe_duration(string $value): ?string
{
    $value = preg_split('/[(（]/u', $value, 2)[0];       // o detalhe entre parênteses não entra
    preg_match('/(\d+)\s*(?:horas?|hours?|小时)/u', $value, $h);
    preg_match('/(\d+)\s*(?:minutos?|minutes?|分钟)/u', $value, $m);
    if (!$h && !$m) {
        return null;
    }
    return 'PT' . ($h ? $h[1] . 'H' : '') . ($m ? $m[1] . 'M' : '');
}

/** Monta o Recipe a partir do HTML do motor; null se a anatomia não estiver completa. */
function sb_recipe_jsonld(string $content, array $d): ?array
{
    $info = [];
    $ingredients = [];
    $steps = [];
    foreach (sb_recipe_sections($content) as $s) {
        if ($s['role'] === 'info' && preg_match_all('/<li>(.*?)<\/li>/su', $s['body'], $lis)) {
            foreach ($lis[1] as $li) {
                if ($pair = sb_recipe_label_value($li)) {
                    $info[mb_strtolower($pair[0])] = $pair[1];
                }
            }
        } elseif ($s['role'] === 'ingredients' && preg_match_all('/<li>(.*?)<\/li>/su', $s['body'], $lis)) {
            foreach ($lis[1] as $li) {
                $ingredients[] = trim(wp_strip_all_tags($li));
            }
        } elseif ($s['role'] === 'steps' && preg_match('/<ol>(.*?)<\/ol>/su', $s['body'], $ol)
                && preg_match_all('/<li>(.*?)<\/li>/su', $ol[1], $lis)) {
            foreach ($lis[1] as $li) {
                $steps[] = ['@type' => 'HowToStep', 'text' => trim(wp_strip_all_tags($li))];
            }
        }
    }
    if (count($ingredients) < 3 || count($steps) < 3) {
        return null;                                      // não parece uma receita completa: fica o Article
    }
    // Espelho de INFO_LABELS do motor (recipe.py): os rótulos da tradução são reescritos
    // por posição com estes textos; mudar lá exige mudar aqui.
    $labels = ['yield' => ['rendimento', 'yield', 'rendimiento', '份量'],
               'prep' => ['tempo de preparo', 'prep time', 'tiempo de preparación', '准备时间'],
               'cook' => ['tempo de cozimento', 'cooking time', 'tiempo de cocción', '烹饪时间'],
               'total' => ['tempo total', 'total time', 'tiempo total', '总时间']];
    $field = static function (string $key) use ($labels, $info): ?string {
        foreach ($labels[$key] as $label) {
            if (isset($info[$label])) {
                return $info[$label];
            }
        }
        return null;
    };
    $recipe = ['@context' => 'https://schema.org', '@type' => 'Recipe', 'name' => $d['title'],
        'description' => $d['description'], 'datePublished' => $d['published'],
        'inLanguage' => str_replace('_', '-', $d['locale']), 'mainEntityOfPage' => $d['url'],
        'author' => ['@type' => 'Organization', 'name' => $d['site']],
        'publisher' => ['@type' => 'Organization', 'name' => $d['site'], 'url' => home_url('/')],
        'recipeIngredient' => $ingredients, 'recipeInstructions' => $steps];
    if ($d['image'] !== '') {
        $recipe['image'] = [$d['image']];
    }
    if ($yield = $field('yield')) {
        $recipe['recipeYield'] = $yield;
    }
    foreach (['prep' => 'prepTime', 'cook' => 'cookTime', 'total' => 'totalTime'] as $key => $prop) {
        $value = $field($key);
        if ($value !== null && ($iso = sb_recipe_duration($value)) !== null) {
            $recipe[$prop] = $iso;
        }
    }
    $category = get_the_category();
    if ($category !== [] && $category[0] instanceof WP_Term) {
        $recipe['recipeCategory'] = $category[0]->name;
    }
    return $recipe;
}

add_filter('sb_seo_jsonld', static function (?array $json, array $d, string $context): ?array {
    if ($context !== 'post' || !sb_recipe_enabled()) {
        return $json;
    }
    $post = get_queried_object();
    if (!$post instanceof WP_Post) {
        return $json;
    }
    return sb_recipe_jsonld($post->post_content, $d) ?? $json;
}, 10, 3);
