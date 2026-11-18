<?php
/**
 * Tema ShortsBlog (visual "Grade"). Sem JavaScript e sem pedidos a terceiros.
 */

if (!defined('ABSPATH')) {
    exit;
}

/** Cor de cada blog: [tema claro, tema escuro]. */
const SB_ACCENTS = [
    'cloud' => ['#2457d6', '#8fb0ff'], 'ia' => ['#6b3fd4', '#b9a2ff'],
    'financas' => ['#1f7a4d', '#6fd3a0'], 'saude' => ['#0f7f86', '#63d0d6'],
    'politica' => ['#9b2c3a', '#f09aa5'], 'culinaria' => ['#c8551b', '#ffab7a'],
];

add_action('after_setup_theme', static function (): void {
    add_theme_support('title-tag');
    add_theme_support('post-thumbnails');
    add_image_size('sb-hero', 1200, 675, true);
    add_image_size('sb-card', 640, 360, true);
    add_theme_support('html5', ['search-form', 'gallery', 'caption', 'style', 'script']);
    register_nav_menus(['footer' => 'Rodapé']);
});

add_action('wp_enqueue_scripts', static function (): void {
    wp_enqueue_style('shortsblog', get_stylesheet_uri(), [], wp_get_theme()->get('Version'));
    $site = defined('SB_SITE_KEY') ? SB_SITE_KEY : 'cloud';
    [$light, $dark] = SB_ACCENTS[$site] ?? SB_ACCENTS['cloud'];
    wp_add_inline_style('shortsblog', ":root{--c:{$light};--c-dark:{$dark}}");
    if (is_readable(get_theme_file_path("$site.css"))) {
        // Variação visual do blog (D-086): carregada quando o tema traz `<site>.css`
        // (hoje só a Culinária); os outros blogs seguem o «Grade» padrão.
        wp_enqueue_style("shortsblog-$site", get_theme_file_uri("$site.css"),
            ['shortsblog'], wp_get_theme()->get('Version'));
    }
    // O blog não usa os blocos do editor no site público: menos CSS.
    wp_dequeue_style('wp-block-library');
    wp_dequeue_style('classic-theme-styles');
}, 20);
remove_action('wp_head', 'print_emoji_detection_script', 7);
remove_action('wp_print_styles', 'print_emoji_styles');
// O CSS `global-styles` não sai com um dequeue (o WP volta a enfileirá-lo):
// tira-se o enqueue dos dois ganchos onde o core o pendura.
remove_action('wp_enqueue_scripts', 'wp_enqueue_global_styles');
remove_action('wp_footer', 'wp_enqueue_global_styles', 1);

/** Os poucos textos do tema, nos 4 idiomas (o idioma vem do Polylang). */
function sb_t(string $key): string
{
    $texts = [
        'skip' => ['pt' => 'Pular para o conteúdo', 'en' => 'Skip to content',
                   'es' => 'Saltar al contenido', 'zh' => '跳至正文'],
        'latest' => ['pt' => 'Mais recentes', 'en' => 'Latest', 'es' => 'Lo más reciente', 'zh' => '最新文章'],
        'featured' => ['pt' => 'Em destaque', 'en' => 'Featured', 'es' => 'Destacado', 'zh' => '精选'],
        'read' => ['pt' => 'Ler artigo', 'en' => 'Read article', 'es' => 'Leer artículo', 'zh' => '阅读全文'],
        'search' => ['pt' => 'Pesquisar', 'en' => 'Search', 'es' => 'Buscar', 'zh' => '搜索'],
        'all' => ['pt' => 'Tudo', 'en' => 'All', 'es' => 'Todo', 'zh' => '全部'],
        'more' => ['pt' => 'Mais artigos', 'en' => 'More articles', 'es' => 'Más artículos', 'zh' => '更多文章'],
        'empty' => ['pt' => 'Ainda não há artigos por aqui.', 'en' => 'No articles here yet.',
                    'es' => 'Todavía no hay artículos aquí.', 'zh' => '这里还没有文章。'],
        'related' => ['pt' => 'Leia também', 'en' => 'Read next', 'es' => 'Lea también', 'zh' => '延伸阅读'],
        // Crédito do rodapé: criado e operado por IA; direitos da Cloud Arquiteto.
        'credit' => ['pt' => 'Este blog é criado e operado por inteligência artificial, da '
                             . 'escolha das pautas à publicação — todos os direitos reservados '
                             . 'a Cloud Arquiteto.',
                     'en' => 'This blog is created and run by artificial intelligence, from '
                             . 'story selection to publishing — all rights reserved to '
                             . 'Cloud Arquiteto.',
                     'es' => 'Este blog es creado y gestionado por inteligencia artificial, de '
                             . 'la elección de los temas a la publicación — todos los derechos '
                             . 'reservados a Cloud Arquiteto.',
                     'zh' => '本博客由人工智能创建并运营，从选题到发布——版权所有 Cloud Arquiteto。'],
        'credit_cta' => ['pt' => 'Conheça o criador →', 'en' => 'Meet the maker →',
                         'es' => 'Conozca al creador →', 'zh' => '了解幕后创造者 →'],
    ];
    $lang = function_exists('pll_current_language') ? (string) pll_current_language('slug') : 'pt';
    return $texts[$key][$lang] ?? $texts[$key]['pt'] ?? $key;
}

/**
 * Nome e tagline de cada blog nos 4 idiomas (pacote visual da revisão 360°).
 * O pt é a fonte de verdade (igual a content-config/sites/<site>.yaml, premissa D-102);
 * os outros idiomas trocam o texto no front via filtros de blogname/blogdescription.
 */
const SB_IDENTITY = [
    'cloud' => [
        'name' => ['pt' => 'Cloud e Infraestrutura', 'en' => 'Cloud & Infrastructure',
                   'es' => 'Nube e Infraestructura', 'zh' => '云与基础设施'],
        'tagline' => [
            'pt' => 'Computação em nuvem, infraestrutura, DevOps e segurança para quem opera sistemas.',
            'en' => 'Cloud computing, infrastructure, DevOps and security for people who run systems.',
            'es' => 'Computación en la nube, infraestructura, DevOps y seguridad para quienes operan sistemas.',
            'zh' => '云计算、基础设施、DevOps 与安全，写给运维系统的人。'],
    ],
    'ia' => [
        'name' => ['pt' => 'Inteligência Artificial', 'en' => 'Artificial Intelligence',
                   'es' => 'Inteligencia Artificial', 'zh' => '人工智能'],
        'tagline' => [
            'pt' => 'Modelos, ferramentas e aplicações práticas de inteligência artificial.',
            'en' => 'Models, tools and practical applications of artificial intelligence.',
            'es' => 'Modelos, herramientas y aplicaciones prácticas de la inteligencia artificial.',
            'zh' => '人工智能的模型、工具与实际应用。'],
    ],
    'financas' => [
        'name' => ['pt' => 'Finanças', 'en' => 'Finance', 'es' => 'Finanzas', 'zh' => '财经'],
        'tagline' => [
            'pt' => 'Finanças pessoais, investimentos e economia no mundo todo: bancos centrais, mercados, moedas e custo de vida de qualquer país.',
            'en' => 'Personal finance, investing and the world economy: central banks, markets, currencies and the cost of living in any country.',
            'es' => 'Finanzas personales, inversiones y economía en todo el mundo: bancos centrales, mercados, monedas y costo de vida de cualquier país.',
            'zh' => '个人理财、投资与全球经济：各国央行、市场、货币与生活成本。'],
    ],
    'saude' => [
        'name' => ['pt' => 'Saúde', 'en' => 'Health', 'es' => 'Salud', 'zh' => '健康'],
        'tagline' => [
            'pt' => 'Saúde, bem-estar e prevenção no mundo todo, com base em fontes médicas e científicas de qualquer país.',
            'en' => 'Health, well-being and prevention worldwide, based on medical and scientific sources from any country.',
            'es' => 'Salud, bienestar y prevención en todo el mundo, con base en fuentes médicas y científicas de cualquier país.',
            'zh' => '面向全球的健康、保健与预防，依据各国的医学与科学来源。'],
    ],
    'politica' => [
        'name' => ['pt' => 'Política', 'en' => 'Politics', 'es' => 'Política', 'zh' => '政治'],
        'tagline' => [
            'pt' => 'Política mundial: eleições, governos, parlamentos e organismos internacionais de qualquer país, com fatos verificáveis e fontes citadas.',
            'en' => 'World politics: elections, governments, parliaments and international bodies in any country, with verifiable facts and cited sources.',
            'es' => 'Política mundial: elecciones, gobiernos, parlamentos y organismos internacionales de cualquier país, con hechos verificables y fuentes citadas.',
            'zh' => '世界政治：各国的选举、政府、议会与国际组织，事实可查、来源可引。'],
    ],
    'culinaria' => [
        'name' => ['pt' => 'Culinária', 'en' => 'Cooking', 'es' => 'Cocina', 'zh' => '美食'],
        'tagline' => [
            'pt' => 'Receitas, técnicas e ingredientes das cozinhas do mundo todo, incluindo receitas originais.',
            'en' => 'Recipes, techniques and ingredients from kitchens around the world, including original recipes.',
            'es' => 'Recetas, técnicas e ingredientes de las cocinas de todo el mundo, incluidas recetas originales.',
            'zh' => '来自世界各地厨房的食谱、技巧与食材，包括原创食谱。'],
    ],
];

/** Nome/tagline no idioma da página, só no front (o admin, o REST e o cron veem a opção gravada). */
function sb_identity(string $field, string $value): string
{
    if (is_admin() || wp_doing_ajax() || wp_doing_cron()
        || (defined('REST_REQUEST') && REST_REQUEST)
        || !function_exists('pll_current_language')) {
        return $value;
    }
    $site = defined('SB_SITE_KEY') ? SB_SITE_KEY : '';
    $lang = (string) pll_current_language('slug');
    return SB_IDENTITY[$site][$field][$lang] ?? $value;
}
add_filter('option_blogname', static fn($value): string => sb_identity('name', (string) $value));
add_filter('option_blogdescription', static fn($value): string => sb_identity('tagline', (string) $value));

/** Data do post no formato de cada idioma. */
function sb_date(): string
{
    $formats = ['pt' => 'j \\d\\e F \\d\\e Y', 'en' => 'F j, Y', 'es' => 'j \\d\\e F \\d\\e Y',
                'zh' => 'Y年n月j日'];
    $lang = function_exists('pll_current_language') ? (string) pll_current_language('slug') : 'pt';
    return get_the_date($formats[$lang] ?? $formats['pt']);
}

/** Primeira categoria do post, como ligação. */
function sb_category_link(): string
{
    $categories = get_the_category();
    if (!$categories) {
        return '';
    }
    return sprintf('<a class="sb-cat" href="%s">%s</a>',
        esc_url(get_category_link($categories[0])), esc_html($categories[0]->name));
}

/** Minutos de leitura (200 palavras por minuto; em chinês conta caracteres). */
function sb_reading_minutes(): int
{
    $text = wp_strip_all_tags(get_the_content());
    $cjk = preg_match_all('/\p{Han}/u', $text);
    $words = $cjk > 200 ? (int) ($cjk / 2.5) : str_word_count(remove_accents($text));
    return max(1, (int) round($words / 200));
}

add_filter('excerpt_length', static fn(): int => 32);
add_filter('excerpt_more', static fn(): string => '…');

/**
 * Até `$limit` artigos para o «Leia também»: mesmo idioma, prefere a(s) mesma(s)
 * categoria(s) e completa com os mais recentes do blog. Sem JavaScript, uma consulta
 * (duas quando a categoria não enche a lista). (Pacote SEO da revisão 360°, 5/11.)
 */
function sb_related_posts(int $limit = 3): array
{
    $post_id = get_the_ID();
    $args = ['post_type' => 'post', 'post_status' => 'publish', 'numberposts' => $limit,
             'post__not_in' => [$post_id], 'suppress_filters' => false,
             'lang' => function_exists('pll_current_language') ? (string) pll_current_language('slug') : ''];
    $cats = wp_get_post_categories($post_id);
    $found = $cats ? get_posts($args + ['category__in' => $cats]) : [];
    if (count($found) < $limit) {
        $seen = array_merge([$post_id], wp_list_pluck($found, 'ID'));
        $found = array_merge($found, get_posts(['post__not_in' => $seen] + $args));
    }
    return array_slice($found, 0, $limit);
}

/** Capa do post: imagem destacada ou, sem ela, um gradiente da cor do blog (variante pelo ID). */
function sb_media(string $size = 'sb-card'): string
{
    if (has_post_thumbnail()) {
        return '<span class="sb-media">' . get_the_post_thumbnail(null, $size, ['loading' => 'lazy', 'alt' => '']) . '</span>';
    }
    $cats = get_the_category();
    $mark = $cats ? mb_substr($cats[0]->name, 0, 1) : mb_substr(get_bloginfo('name'), 0, 1);
    return sprintf('<span class="sb-media sb-ph v%d" aria-hidden="true"><b>%s</b></span>',
        get_the_ID() % 4, esc_html(mb_strtoupper($mark)));
}

/**
 * Rodapé sem menu configurado: lista as páginas do blog (Sobre, Política editorial, Privacidade, Contato) no
 * idioma atual. As páginas levam o meta `_sb_page` (criadas por `setup/pages.php`); sem elas, não mostra nada.
 */
function sb_footer_links(): void
{
    $pages = get_posts([
        'post_type' => 'page', 'post_status' => 'publish', 'meta_key' => '_sb_page', 'numberposts' => 10,
        'orderby' => 'menu_order', 'order' => 'ASC', 'suppress_filters' => false,
        'lang' => function_exists('pll_current_language') ? (string) pll_current_language('slug') : '',
    ]);
    if (!$pages) {
        return;
    }
    echo '<ul class="menu">';
    foreach ($pages as $page) {
        echo '<li><a href="' . esc_url(get_permalink($page)) . '">' . esc_html(get_the_title($page)) . '</a></li>';
    }
    echo '</ul>';
}
