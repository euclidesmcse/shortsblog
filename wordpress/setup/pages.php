<?php
/**
 * Páginas do blog (Sobre, Política editorial, Privacidade, Contato) nos 4 idiomas, ligadas entre si (E8.6).
 * `wp eval-file /setup/pages.php` com SB_SITE_KEY e, opcionalmente, SB_PAGES_STATUS (`draft` por omissão, ou
 * `publish`), SB_PAGES_DRY=1 (mostra o que faria, sem escrever) e SB_PAGES_ONLY=<página> (só essa). Pode repetir-se: atualiza as páginas que já
 * existem (procura pelo meta `_sb_page` e pelo idioma) e não cria duplicados.
 *
 * Modelos em /pages/<página>.<idioma>.html e variáveis em /pages/sites.json (ver wordpress/pages/README.md).
 */

$site = getenv('SB_SITE_KEY') ?: (defined('SB_SITE_KEY') ? SB_SITE_KEY : '');
$status = getenv('SB_PAGES_STATUS') ?: 'draft';
$dry = getenv('SB_PAGES_DRY') === '1';
if (!in_array($status, ['draft', 'publish'], true)) {
    WP_CLI::error("SB_PAGES_STATUS inválido: $status");
}
$vars = json_decode((string) file_get_contents('/pages/sites.json'), true);
if (!is_array($vars) || empty($vars['sites'][$site])) {
    WP_CLI::error("sem variáveis para o site '$site' em /pages/sites.json");
}
$global = $vars['global'];
$mine = $vars['sites'][$site];
$engine_lang = ['pt' => 'pt-BR', 'en' => 'en', 'es' => 'es', 'zh' => 'zh-CN'];
$order = ['sobre' => 1, 'politica-editorial' => 2, 'privacidade' => 3, 'contato' => 4];
$only = getenv('SB_PAGES_ONLY') ?: '';
if ($only !== '' && !isset($order[$only])) {
    WP_CLI::error("SB_PAGES_ONLY inválido: $only");
}

foreach ($global as $key => $value) {
    $text = is_array($value) ? implode(' ', $value) : (string) $value;
    if (strpos($text, 'A-PREENCHER') !== false || strpos($text, 'TO-FILL') !== false) {
        WP_CLI::error("variável global por preencher: $key (wordpress/pages/sites.json)");
    }
}

function sb_page_fill(string $template, array $mine, array $global, string $lang, string $site): string
{
    $replace = [
        '{{SITE_NAME}}' => $mine['name'][$lang], '{{TOPIC}}' => $mine['topic'][$lang],
        '{{SITE_URL}}' => "https://$site.shortsblog.com",
        '{{AREA_ABOUT}}' => $mine['area_about'][$lang], '{{AREA_POLICY}}' => $mine['area_policy'][$lang],
        '{{AREA_CONTACT}}' => $mine['area_contact'][$lang],
        '{{CONTACT_EMAIL}}' => $global['CONTACT_EMAIL'],
    ];
    foreach (['OPERATOR', 'UPDATED', 'LOG_RETENTION'] as $key) {
        $replace['{{' . $key . '}}'] = $global[$key][$lang];
    }
    $out = strtr($template, $replace);
    if (preg_match('/\{\{[A-Z_]+\}\}/', $out, $left)) {
        WP_CLI::error("marcador por substituir ($lang): " . $left[0]);
    }
    return $out;
}

$made = 0;
foreach ($order as $page => $menu_order) {
    if ($only !== '' && $page !== $only) {
        continue;
    }
    $ids = [];
    foreach ($engine_lang as $slug => $lang) {
        $file = "/pages/$page.$lang.html";
        $raw = (string) file_get_contents($file);
        if (!preg_match('/^<!-- title: (.*?) -->\n<!-- slug: (.*?) -->\n(.*)$/s', $raw, $m)) {
            WP_CLI::error("formato inesperado em $file");
        }
        $title = sb_page_fill($m[1], $mine, $global, $lang, $site);
        $content = trim(sb_page_fill($m[3], $mine, $global, $lang, $site));
        $existing = get_posts([
            'post_type' => 'page', 'post_status' => 'any', 'numberposts' => 1, 'meta_key' => '_sb_page',
            'meta_value' => $page, 'lang' => $slug, 'suppress_filters' => false, 'fields' => 'ids',
        ]);
        $args = ['post_type' => 'page', 'post_title' => $title, 'post_name' => $m[2], 'post_content' => $content,
                 'post_status' => $status, 'menu_order' => $menu_order];
        if ($dry) {
            WP_CLI::log(($existing ? 'atualizaria' : 'criaria') . " $page [$slug] «{$title}» ($status, " . strlen($content) . ' bytes)');
            continue;
        }
        if ($existing) {
            $id = wp_update_post($args + ['ID' => $existing[0]], true);
        } else {
            $id = wp_insert_post($args, true);
            $made++;
        }
        if (is_wp_error($id)) {
            WP_CLI::error("$page [$slug]: " . $id->get_error_message());
        }
        update_post_meta($id, '_sb_page', $page);
        pll_set_post_language($id, $slug);
        $ids[$slug] = (int) $id;
    }
    if (!$dry) {
        pll_save_post_translations($ids);
    }
}
if (!$dry) {
    WP_CLI::success("$site: " . ($only ?: "4 páginas") . " × 4 idiomas ($made novas), estado $status");
}
