<?php
/**
 * Idiomas do Polylang (E8.4): pt-BR é o padrão, sem prefixo; /en/, /es/, /zh/.
 * Corre com `wp eval-file`. Pode repetir-se.
 */

if (!function_exists('PLL') || !PLL()->model) {
    WP_CLI::error('Polylang não está carregado.');
}

$wanted = [
    ['name' => 'Português', 'slug' => 'pt', 'locale' => 'pt_BR', 'rtl' => false, 'term_group' => 0, 'flag' => 'br'],
    ['name' => 'English', 'slug' => 'en', 'locale' => 'en_US', 'rtl' => false, 'term_group' => 1, 'flag' => 'us'],
    ['name' => 'Español', 'slug' => 'es', 'locale' => 'es_ES', 'rtl' => false, 'term_group' => 2, 'flag' => 'es'],
    ['name' => '中文', 'slug' => 'zh', 'locale' => 'zh_CN', 'rtl' => false, 'term_group' => 3, 'flag' => 'cn'],
];
$model = PLL()->model;
$existing = wp_list_pluck($model->get_languages_list(), 'slug');
foreach ($wanted as $language) {
    if (in_array($language['slug'], $existing, true)) {
        continue;
    }
    $result = $model->add_language($language);
    if (is_wp_error($result)) {
        WP_CLI::error("idioma {$language['slug']}: " . $result->get_error_message());
    }
    WP_CLI::log("idioma criado: {$language['slug']}");
}

$options = get_option('polylang', []);
$options['default_lang'] = 'pt';
$options['force_lang'] = 1;        // idioma pelo diretório: /en/…
$options['hide_default'] = 1;      // o português fica sem prefixo
$options['rewrite'] = 1;           // sem /language/ no endereço
$options['redirect_lang'] = 0;
$options['browser'] = 0;           // não redireciona pelo idioma do browser (o Cloudflare guarda cache)
$options['media_support'] = 1;     // media com idioma (alt por idioma, E9.3)
$options['post_types'] = [];
$options['taxonomies'] = [];
update_option('polylang', $options);

$model->clean_languages_cache();
// O que já existir sem idioma fica em português.
if (method_exists($model, 'set_language_in_mass')) {
    $model->set_language_in_mass();
}
WP_CLI::success('Polylang: ' . implode(', ', wp_list_pluck($model->get_languages_list(), 'slug')));
