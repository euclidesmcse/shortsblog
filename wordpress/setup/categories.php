<?php
/**
 * Categorias do blog nos 4 idiomas, ligadas entre si (E8.6).
 * `wp eval-file /setup/categories.php` com SB_SITE_KEY definido. Pode repetir-se.
 * Os nomes em português têm de ser os de `content-config/sites/<site>.yaml` (o motor escolhe por nome).
 */

$file = '/sites/' . SB_SITE_KEY . '.json';
$data = json_decode((string) file_get_contents($file), true);
if (!is_array($data) || empty($data['categories'])) {
    WP_CLI::error("sem categorias em $file");
}
$languages = ['pt', 'en', 'es', 'zh'];
$created = 0;
foreach ($data['categories'] as $category) {
    $ids = [];
    foreach ($languages as $lang) {
        // O slug é único por taxonomia: o português fica com o slug base, os outros levam o idioma.
        $slug = $lang === 'pt' ? $category['slug'] : $category['slug'] . '-' . $lang;
        $term = get_term_by('slug', $slug, 'category');
        if (!$term) {
            $result = wp_insert_term($category[$lang], 'category', ['slug' => $slug]);
            if (is_wp_error($result)) {
                WP_CLI::error("{$category[$lang]} ($lang): " . $result->get_error_message());
            }
            $term_id = (int) $result['term_id'];
            $created++;
        } else {
            $term_id = (int) $term->term_id;
            if ($term->name !== $category[$lang]) {
                wp_update_term($term_id, 'category', ['name' => $category[$lang]]);
            }
        }
        pll_set_term_language($term_id, $lang);
        $ids[$lang] = $term_id;
    }
    pll_save_term_translations($ids);
}
// A categoria padrão do WordPress ("Sem categoria") passa a ser "Notícias" em cada idioma.
$default = get_term_by('slug', $data['categories'][0]['slug'], 'category');
if ($default) {
    update_option('default_category', (int) $default->term_id);
    // …e as "Sem categoria" que o WordPress e o Polylang criaram (uma por idioma) saem, se vazias.
    foreach (get_terms(['taxonomy' => 'category', 'hide_empty' => false, 'lang' => '']) as $term) {
        if ((int) $term->count === 0
            && preg_match('/^(sem-categoria|uncategorized)(-[a-z]{2})?$/', $term->slug)) {
            wp_delete_term((int) $term->term_id, 'category');
        }
    }
}
WP_CLI::success(sprintf('%d categorias × %d idiomas (%d novas)', count($data['categories']),
    count($languages), $created));
