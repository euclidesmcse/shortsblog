<?php
/**
 * Plugin Name: ShortsBlog — Idiomas e job na REST
 * Description: O Polylang gratuito não expõe o idioma nem as traduções na REST. Este plugin
 *              acrescenta `lang` e `translations` a posts, páginas, media, categorias e
 *              etiquetas, e o campo `sb_job_id` (o job do motor que criou o post). (E8.4)
 *
 * Ler:      qualquer pedido vê `lang` e `translations`; `sb_job_id` só quem pode editar.
 * Escrever: só quem pode editar esse post ou gerir termos (a REST já o exige).
 *           `translations` é um objeto {idioma: id}; o próprio item entra sempre no seu idioma.
 */

if (!defined('ABSPATH')) {
    exit;
}

const SB_JOB_META = 'sb_job_id';
const SB_POST_TYPES = ['post', 'page', 'attachment'];
const SB_TAXONOMIES = ['category', 'post_tag'];

/** Idiomas configurados no Polylang (slugs: pt, en, es, zh). */
function sb_i18n_languages(): array
{
    return function_exists('pll_languages_list') ? (array) pll_languages_list() : [];
}

function sb_i18n_check_lang($value)
{
    $value = sanitize_key((string) $value);
    if (!in_array($value, sb_i18n_languages(), true)) {
        return new WP_Error('sb_invalid_lang', 'Idioma desconhecido.', ['status' => 400]);
    }
    return $value;
}

/** {idioma: id} só com idiomas conhecidos e ids que existem e são do tipo certo. */
function sb_i18n_clean_translations($value, callable $exists)
{
    if (!is_array($value)) {
        return new WP_Error('sb_invalid_translations', 'translations tem de ser um objeto.',
            ['status' => 400]);
    }
    $clean = [];
    foreach ($value as $lang => $id) {
        $lang = sanitize_key((string) $lang);
        $id = absint($id);
        if (!in_array($lang, sb_i18n_languages(), true) || !$id || !$exists($id)) {
            return new WP_Error('sb_invalid_translations',
                'translations: idioma desconhecido ou id inexistente.', ['status' => 400]);
        }
        $clean[$lang] = $id;
    }
    return $clean;
}

add_action('rest_api_init', static function (): void {
    if (!function_exists('pll_set_post_language')) {
        return;                                   // Polylang ainda não está ativo
    }

    foreach (SB_POST_TYPES as $type) {
        register_rest_field($type, 'lang', [
            'get_callback' => static fn(array $post) => pll_get_post_language((int) $post['id'], 'slug') ?: null,
            'update_callback' => static function ($value, WP_Post $post) {
                $lang = sb_i18n_check_lang($value);
                if (is_wp_error($lang)) {
                    return $lang;
                }
                pll_set_post_language($post->ID, $lang);
                return true;
            },
            'schema' => ['type' => ['string', 'null'], 'context' => ['view', 'edit']],
        ]);
        register_rest_field($type, 'translations', [
            'get_callback' => static fn(array $post) => (object) pll_get_post_translations((int) $post['id']),
            'update_callback' => static function ($value, WP_Post $post) {
                $clean = sb_i18n_clean_translations($value, static function (int $id) use ($post): bool {
                    $other = get_post($id);
                    return $other && $other->post_type === $post->post_type
                        && current_user_can('edit_post', $id);
                });
                if (is_wp_error($clean)) {
                    return $clean;
                }
                $own = pll_get_post_language($post->ID, 'slug');
                if ($own) {
                    $clean[$own] = $post->ID;
                }
                pll_save_post_translations($clean);
                return true;
            },
            'schema' => ['type' => 'object', 'context' => ['view', 'edit'],
                         'additionalProperties' => ['type' => 'integer']],
        ]);
    }

    foreach (SB_TAXONOMIES as $taxonomy) {
        register_rest_field($taxonomy, 'lang', [
            'get_callback' => static fn(array $term) => pll_get_term_language((int) $term['id'], 'slug') ?: null,
            'update_callback' => static function ($value, WP_Term $term) {
                $lang = sb_i18n_check_lang($value);
                if (is_wp_error($lang)) {
                    return $lang;
                }
                pll_set_term_language($term->term_id, $lang);
                return true;
            },
            'schema' => ['type' => ['string', 'null'], 'context' => ['view', 'edit']],
        ]);
        register_rest_field($taxonomy, 'translations', [
            'get_callback' => static fn(array $term) => (object) pll_get_term_translations((int) $term['id']),
            'update_callback' => static function ($value, WP_Term $term) {
                $clean = sb_i18n_clean_translations($value, static function (int $id) use ($term): bool {
                    $other = get_term($id, $term->taxonomy);
                    return $other instanceof WP_Term;
                });
                if (is_wp_error($clean)) {
                    return $clean;
                }
                $own = pll_get_term_language($term->term_id, 'slug');
                if ($own) {
                    $clean[$own] = $term->term_id;
                }
                pll_save_term_translations($clean);
                return true;
            },
            'schema' => ['type' => 'object', 'context' => ['view', 'edit'],
                         'additionalProperties' => ['type' => 'integer']],
        ]);
    }
});

// `sb_job_id`: liga o post ao job do motor (idempotência do publicador, E9.4). Não é registado
// com `show_in_rest` porque isso o mostraria a toda a gente; vai num campo próprio, que só
// aparece a quem pode editar o post.
add_action('init', static function (): void {
    foreach (['post', 'page'] as $type) {
        register_post_meta($type, SB_JOB_META, [
            'type' => 'integer', 'single' => true, 'show_in_rest' => false,
            'sanitize_callback' => 'absint',
            'auth_callback' => static fn() => current_user_can('edit_posts'),
        ]);
    }
});
add_action('rest_api_init', static function (): void {
    foreach (['post', 'page'] as $type) {
        register_rest_field($type, SB_JOB_META, [
            'get_callback' => static function (array $post) {
                if (!current_user_can('edit_post', (int) $post['id'])) {
                    return null;
                }
                $value = get_post_meta((int) $post['id'], SB_JOB_META, true);
                return $value === '' ? null : (int) $value;
            },
            'update_callback' => static function ($value, WP_Post $post) {
                if (!current_user_can('edit_post', $post->ID)) {
                    return new WP_Error('sb_forbidden', 'Sem permissão.', ['status' => 403]);
                }
                update_post_meta($post->ID, SB_JOB_META, absint($value));
                return true;
            },
            'schema' => ['type' => ['integer', 'null'], 'context' => ['view', 'edit']],
        ]);
    }
    // Procurar pelo job: GET /wp/v2/posts?sb_job_id=123&status=any (só autenticado).
    foreach (['post', 'page'] as $type) {
        add_filter("rest_{$type}_query", static function (array $args, WP_REST_Request $request): array {
            $job = absint($request->get_param(SB_JOB_META));
            if ($job && current_user_can('edit_posts')) {
                $args['meta_query'][] = ['key' => SB_JOB_META, 'value' => $job, 'type' => 'NUMERIC'];
            }
            return $args;
        }, 10, 2);
    }
});
