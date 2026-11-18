<?php
/**
 * Plugin Name: ShortsBlog — Endurecimento
 * Description: Fecha o que o blog não usa (E8.8): XML-RPC, enumeração de utilizadores,
 *              versão do WordPress e do PHP, comentários, cabeçalhos de segurança, ficheiros
 *              que revelam a versão e limite de tentativas no login (R45).
 */

if (!defined('ABSPATH')) {
    exit;
}

// XML-RPC: o motor publica pela REST com Application Password; isto fica fechado. Os filtros
// abaixo só desligam os métodos com autenticação — os `system.*` continuariam a responder —,
// por isso o pedido é recusado logo à entrada.
if (defined('XMLRPC_REQUEST') && XMLRPC_REQUEST) {
    status_header(403);
    header('Content-Type: text/plain; charset=utf-8');
    exit('XML-RPC desativado.');
}
add_filter('xmlrpc_enabled', '__return_false');
add_filter('xmlrpc_methods', '__return_empty_array');
add_filter('wp_headers', static function (array $headers): array {
    unset($headers['X-Pingback']);
    return $headers;
});
add_filter('pings_open', '__return_false');

// Sem a versão do WordPress no HTML e nos feeds.
remove_action('wp_head', 'wp_generator');
add_filter('the_generator', '__return_empty_string');
remove_action('wp_head', 'rsd_link');
remove_action('wp_head', 'wlwmanifest_link');

// Lista de utilizadores na REST só para quem está autenticado.
add_filter('rest_endpoints', static function (array $endpoints): array {
    if (is_user_logged_in()) {
        return $endpoints;
    }
    foreach (array_keys($endpoints) as $route) {
        if (strpos($route, '/wp/v2/users') === 0) {
            unset($endpoints[$route]);
        }
    }
    return $endpoints;
});
// …e sem ?author=N a revelar nomes de utilizador.
add_action('template_redirect', static function (): void {
    if (is_author() || isset($_GET['author'])) {
        wp_safe_redirect(home_url('/'), 301);
        exit;
    }
});

// Os blogs não têm comentários.
add_filter('comments_open', '__return_false', 20);
add_filter('comments_array', '__return_empty_array', 20);
add_action('admin_menu', static function (): void {
    remove_menu_page('edit-comments.php');
});

// --- Cabeçalhos de segurança (E8.8) ---------------------------------------------------------
// O tema não usa JavaScript, fontes nem imagens de terceiros: a CSP pode ser estrita. Só para quem
// não está autenticado (a barra de administração e o editor precisam de scripts).
add_filter('wp_headers', static function (array $headers): array {
    $headers['X-Content-Type-Options'] = 'nosniff';
    $headers['Referrer-Policy'] = 'strict-origin-when-cross-origin';
    $headers['X-Frame-Options'] = 'SAMEORIGIN';
    $headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=(), payment=(), usb=()';
    // Sem `includeSubDomains`: o resto de *.shortsblog.com tem a sua própria política.
    $headers['Strict-Transport-Security'] = 'max-age=15552000';
    if (!is_user_logged_in()) {
        $headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' data:; "
            . "style-src 'self' 'unsafe-inline'; script-src 'none'; object-src 'none'; "
            . "base-uri 'self'; form-action 'self'; frame-ancestors 'self'";
    }
    return $headers;
});
add_action('send_headers', static function (): void {
    header_remove('X-Powered-By');                // não diz a versão do PHP
});

// Ficheiros do WordPress que dizem a versão, e PHP dentro de `uploads/`. O Apache serve-os sem passar
// pelo PHP, por isso vão para o `.htaccess` (escrito por `wp rewrite flush --hard`).
add_filter('mod_rewrite_rules', static function (string $rules): string {
    $extra = "# ShortsBlog (E8.8)\n<IfModule mod_rewrite.c>\nRewriteEngine On\n"
        . "RewriteRule ^(readme\\.html|license\\.txt|wp-config-sample\\.php)$ - [R=404,L]\n"
        . "RewriteRule ^wp-content/uploads/.*\\.(php|phtml|phar)$ - [F,L]\n</IfModule>\n";
    return $extra . $rules;
});

// --- Limite de tentativas no login (R45) ----------------------------------------------------
// 5 falhas em 15 minutos bloqueiam esse IP no `wp-login.php` durante 15 minutos (uma entrada certa
// também é recusada enquanto dura o bloqueio). Não se aplica à REST: a credencial do motor é uma
// Application Password e um erro de configuração não pode bloqueá-la depois de corrigido.
const SB_LOGIN_MAX = 5;
const SB_LOGIN_WINDOW = 900;

/** IP do visitante: `CF-Connecting-IP` só vale quando o pedido chegou por um proxy (IP privado). */
function sb_login_ip(): string
{
    $remote = (string) ($_SERVER['REMOTE_ADDR'] ?? '');
    $cf = (string) ($_SERVER['HTTP_CF_CONNECTING_IP'] ?? '');
    $public = filter_var($remote, FILTER_VALIDATE_IP, FILTER_FLAG_NO_PRIV_RANGE | FILTER_FLAG_NO_RES_RANGE);
    if ($public === false && $cf !== '' && filter_var($cf, FILTER_VALIDATE_IP)) {
        return $cf;
    }
    return $remote;
}

function sb_login_key(): string
{
    return 'sb_login_' . md5(sb_login_ip());
}

add_action('wp_login_failed', static function (): void {
    set_transient(sb_login_key(), (int) get_transient(sb_login_key()) + 1, SB_LOGIN_WINDOW);
});
add_filter('authenticate', static function ($user, $username = '') {
    if ($username !== '' && (int) get_transient(sb_login_key()) >= SB_LOGIN_MAX) {
        return new WP_Error('sb_locked', 'bloqueado');  // depois dos outros filtros: nem a senha certa entra
    }
    return $user;
}, 100, 2);
add_action('wp_login', static function (): void {
    delete_transient(sb_login_key());
});
// Uma só mensagem, para não revelar se o utilizador existe.
add_filter('login_errors', static fn() => 'Não foi possível entrar.');
