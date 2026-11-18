<?php
/**
 * Plugin Name: ShortsBlog — SEO básico
 * Description: Meta description, Open Graph, Twitter Cards, JSON-LD (Article e WebSite) e `x-default` no
 *              hreflang do Polylang. Código próprio, sem plugin de terceiros (D-049). O `canonical`, o
 *              `hreflang` por idioma e os sitemaps vêm do WordPress e do Polylang. (E8.7)
 *
 * Nada aqui muda o que é indexável: `blog_public` continua a decidir (0 até à E14).
 */

if (!defined('ABSPATH')) {
    exit;
}

/** Texto simples com no máximo `$max` caracteres (conta caracteres, não bytes: serve para o chinês). */
function sb_seo_trim(string $html, int $max = 160): string
{
    $text = trim(preg_replace('/\s+/u', ' ', wp_strip_all_tags(strip_shortcodes($html))) ?? '');
    if (mb_strlen($text) <= $max) {
        return $text;
    }
    $cut = mb_substr($text, 0, $max - 1);
    $space = mb_strrpos($cut, ' ');
    if ($space !== false && $space > $max * 0.6) {
        $cut = mb_substr($cut, 0, $space);
    }
    return rtrim($cut, " ,;:.-") . '…';
}

/** Locale do Open Graph de um idioma do Polylang (pt → pt_BR, en → en_US, es → es_ES, zh → zh_CN). */
function sb_seo_locale(string $slug): string
{
    $map = ['pt' => 'pt_BR', 'en' => 'en_US', 'es' => 'es_ES', 'zh' => 'zh_CN'];
    return $map[$slug] ?? 'pt_BR';
}

/** Dados de uma página, só leitura (testável sem saída HTML). */
function sb_seo_data(?WP_Post $post, string $context): array
{
    $site = get_bloginfo('name');
    $data = ['site' => $site, 'type' => 'website', 'locale' => 'pt_BR', 'alternates' => [], 'image' => ''];
    $lang = function_exists('pll_current_language') ? (string) pll_current_language('slug') : 'pt';

    if ($context === 'post' && $post) {
        $lang = function_exists('pll_get_post_language') ? (string) pll_get_post_language($post->ID, 'slug') : $lang;
        $excerpt = $post->post_excerpt !== '' ? $post->post_excerpt : $post->post_content;
        $data = array_merge($data, [
            'type' => 'article',
            'title' => get_the_title($post),
            'description' => sb_seo_trim($excerpt),
            'url' => get_permalink($post),
            'published' => get_post_time('c', true, $post),
            'modified' => get_post_modified_time('c', true, $post),
            'image' => (string) get_the_post_thumbnail_url($post, 'sb-hero'),
        ]);
        if (function_exists('pll_get_post_translations')) {
            foreach (pll_get_post_translations($post->ID) as $other_lang => $other_id) {
                if ($other_id !== $post->ID) {
                    $data['alternates'][] = sb_seo_locale((string) $other_lang);
                }
            }
        }
    } elseif ($context === 'category') {
        $term = get_queried_object();
        $name = $term instanceof WP_Term ? $term->name : '';
        $data = array_merge($data, [
            'title' => $name . ' – ' . $site,
            'description' => sb_seo_trim($term instanceof WP_Term && $term->description !== ''
                ? $term->description : $name . ' – ' . get_bloginfo('description')),
            'url' => $term instanceof WP_Term ? get_term_link($term) : home_url('/'),
        ]);
    } else {
        $data = array_merge($data, ['title' => $site, 'description' => sb_seo_trim(get_bloginfo('description')),
                  'url' => function_exists('pll_home_url') ? pll_home_url($lang) : home_url('/')]);
    }
    $data['locale'] = sb_seo_locale($lang);
    $data['lang'] = $lang;
    return $data;
}

function sb_seo_jsonld(array $d, string $context): ?array
{
    if ($context === 'post') {
        $graph = ['@context' => 'https://schema.org', '@type' => 'Article', 'headline' => $d['title'],
            'description' => $d['description'], 'datePublished' => $d['published'],
            'dateModified' => $d['modified'], 'inLanguage' => str_replace('_', '-', $d['locale']),
            'mainEntityOfPage' => $d['url'],
            'author' => ['@type' => 'Organization', 'name' => $d['site']],
            'publisher' => ['@type' => 'Organization', 'name' => $d['site'], 'url' => home_url('/')]];
        // Logo do publisher (rich results): o ícone do site, quando configurado.
        $logo = get_site_icon_url(512);
        if ($logo) {
            $graph['publisher']['logo'] = ['@type' => 'ImageObject', 'url' => $logo];
        }
        if ($d['image'] !== '') {
            $graph['image'] = [$d['image']];
        }
        return $graph;
    }
    if ($context === 'front') {
        return ['@context' => 'https://schema.org', '@type' => 'WebSite', 'name' => $d['site'],
            'url' => $d['url'], 'inLanguage' => str_replace('_', '-', $d['locale'])];
    }
    return null;
}

/** Trilha Home → Categoria → Artigo em JSON-LD (BreadcrumbList; pacote SEO da revisão 360°). */
function sb_seo_breadcrumb_jsonld(?WP_Post $post, array $d): ?array
{
    if (!$post) {
        return null;
    }
    $home = function_exists('pll_home_url') ? pll_home_url($d['lang']) : home_url('/');
    $items = [['@type' => 'ListItem', 'position' => 1, 'name' => $d['site'], 'item' => $home]];
    $categories = get_the_category($post->ID);
    if ($categories) {
        $link = get_category_link($categories[0]);
        if (!is_wp_error($link)) {
            $items[] = ['@type' => 'ListItem', 'position' => 2,
                        'name' => $categories[0]->name, 'item' => $link];
        }
    }
    $items[] = ['@type' => 'ListItem', 'position' => count($items) + 1,
                'name' => $d['title'], 'item' => $d['url']];
    return ['@context' => 'https://schema.org', '@type' => 'BreadcrumbList',
            'itemListElement' => $items];
}

/** O HTML do <head>: tudo escapado; o JSON-LD sem `</script>` possível (JSON_HEX_TAG). */
function sb_seo_head_html(?WP_Post $post, string $context): string
{
    $d = sb_seo_data($post, $context);
    if (empty($d['description']) || empty($d['title'])) {
        return '';
    }
    $out = '<meta name="description" content="' . esc_attr($d['description']) . "\">\n";
    $og = ['og:type' => $d['type'], 'og:site_name' => $d['site'], 'og:title' => $d['title'],
           'og:description' => $d['description'], 'og:url' => $d['url'], 'og:locale' => $d['locale']];
    if ($d['image'] !== '') {
        $og['og:image'] = $d['image'];
    }
    if ($context === 'post') {
        $og['article:published_time'] = $d['published'];
        $og['article:modified_time'] = $d['modified'];
    }
    foreach ($og as $property => $value) {
        $out .= '<meta property="' . esc_attr($property) . '" content="' . esc_attr((string) $value) . "\">\n";
    }
    foreach ($d['alternates'] as $locale) {
        $out .= '<meta property="og:locale:alternate" content="' . esc_attr($locale) . "\">\n";
    }
    $out .= '<meta name="twitter:card" content="' . ($d['image'] !== '' ? 'summary_large_image' : 'summary') . "\">\n";
    // O sb-recipe.php troca o Article por Recipe nos posts de receita da Culinária (D-092).
    $json = apply_filters('sb_seo_jsonld', sb_seo_jsonld($d, $context), $d, $context);
    if ($json) {
        $out .= '<script type="application/ld+json">'
            . wp_json_encode($json, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_HEX_TAG) . "</script>\n";
    }
    if ($context === 'post') {
        // Nó separado do Article/Recipe de propósito: o filtro `sb_seo_jsonld` (sb-recipe)
        // continua a ver e trocar só o nó principal.
        $crumb = sb_seo_breadcrumb_jsonld($post, $d);
        if ($crumb) {
            $out .= '<script type="application/ld+json">'
                . wp_json_encode($crumb, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_HEX_TAG) . "</script>\n";
        }
    }
    return $out;
}

add_action('wp_head', static function (): void {
    if (is_singular('post')) {
        $context = 'post';
    } elseif (is_category()) {
        $context = 'category';
    } elseif (is_front_page() || is_home()) {
        $context = 'front';
    } else {
        return;                                   // páginas, busca e 404: sem meta própria
    }
    echo sb_seo_head_html($context === 'post' ? get_queried_object() : null, $context); // phpcs:ignore WordPress.Security.EscapeOutput
}, 5);

// `x-default` aponta para o idioma padrão (pt-BR, sem prefixo), como o Google recomenda.
add_filter('pll_rel_hreflang_attributes', static function (array $hreflangs): array {
    $default = function_exists('pll_default_language') ? pll_default_language('slug') : 'pt';
    foreach ($hreflangs as $code => $url) {
        if ($code === $default) {
            $hreflangs['x-default'] = $url;
        }
    }
    return $hreflangs;
});

// O sitemap de utilizadores listaria as páginas de autor (contas técnicas e do administrador): fica de fora.
add_filter('wp_sitemaps_add_provider', static fn($provider, string $name) => $name === 'users' ? false : $provider, 10, 2);
