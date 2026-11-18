<?php
/**
 * Plugin Name: ShortsBlog — Newsletter
 * Description: Formulário de inscrição, páginas de confirmação e de saída e webhook do Brevo da
 *              newsletter diária (E17, D-062). Quem guarda os assinantes e envia é o motor; o leitor
 *              só fala com o blog, e o blog fala com o motor pela rede interna, com o token próprio
 *              da newsletter.
 *
 * Tudo o que é dinâmico fica em `/wp-json/sb/v1/newsletter/…`: o Cloudflare não guarda `/wp-json/`
 * em cache (regra `sb-bypass-wp`). Os GET dos links só mostram um botão; quem confirma ou tira a
 * inscrição é o POST (os scanners de e-mail seguem os GET). A saída aceita também o POST de 1
 * clique dos clientes de e-mail (`List-Unsubscribe=One-Click`, RFC 8058).
 *
 * Sem `SB_NEWSLETTER_API` e `SB_NEWSLETTER_TOKEN`, ou com a newsletter desligada no motor, o
 * formulário não aparece.
 */

if (!defined('ABSPATH')) {
    exit;
}

/** Idioma do Polylang → idioma do motor. */
const SB_NL_LANGS = ['pt' => 'pt-BR', 'en' => 'en', 'es' => 'es', 'zh' => 'zh-CN'];

/** Textos nos 4 idiomas. */
function sb_nl_t(string $key, string $lang): string
{
    static $texts = [
        'title' => ['pt' => 'Newsletter diária', 'en' => 'Daily newsletter', 'es' => 'Boletín diario',
                    'zh' => '每日简报'],
        'pitch' => ['pt' => 'Receba por e-mail, uma vez por dia, os links dos posts publicados nos blogs ShortsBlog.',
                    'en' => 'Get the links to the posts published on the ShortsBlog blogs by e-mail, once a day.',
                    'es' => 'Recibe por correo, una vez al día, los enlaces de los posts publicados en los blogs ShortsBlog.',
                    'zh' => '每天一次通过邮件接收 ShortsBlog 各博客发布文章的链接。'],
        'email' => ['pt' => 'Seu e-mail', 'en' => 'Your e-mail', 'es' => 'Tu correo', 'zh' => '您的邮箱'],
        'button' => ['pt' => 'Assinar', 'en' => 'Subscribe', 'es' => 'Suscribirme', 'zh' => '订阅'],
        'consent' => ['pt' => 'Ao assinar, você concorda em receber a newsletter e com a nossa',
                      'en' => 'By subscribing, you agree to receive the newsletter and to our',
                      'es' => 'Al suscribirte, aceptas recibir el boletín y nuestra',
                      'zh' => '订阅即表示您同意接收简报并同意我们的'],
        'privacy' => ['pt' => 'política de privacidade', 'en' => 'privacy policy',
                      'es' => 'política de privacidad', 'zh' => '隐私政策'],
        'leave_any' => ['pt' => 'Você pode sair quando quiser, com 1 clique no rodapé de cada e-mail.',
                        'en' => 'You can leave at any time with 1 click in the footer of every e-mail.',
                        'es' => 'Puedes darte de baja cuando quieras, con 1 clic al pie de cada correo.',
                        'zh' => '您可以随时通过每封邮件底部的链接一键退订。'],
        'check_mail' => ['pt' => 'Quase lá! Se o endereço estiver certo, você vai receber um e-mail para confirmar a inscrição.',
                         'en' => 'Almost there! If the address is correct, you will receive an e-mail to confirm your subscription.',
                         'es' => '¡Casi listo! Si la dirección es correcta, recibirás un correo para confirmar la suscripción.',
                         'zh' => '就快完成了!如果地址正确,您将收到一封确认订阅的邮件。'],
        'bad_email' => ['pt' => 'Esse e-mail não parece válido. Volte e tente de novo.',
                        'en' => "That e-mail address doesn't look valid. Go back and try again.",
                        'es' => 'Ese correo no parece válido. Vuelve e inténtalo de nuevo.',
                        'zh' => '该邮箱地址似乎无效,请返回重试。'],
        'try_later' => ['pt' => 'Não foi possível concluir agora. Tente de novo mais tarde.',
                        'en' => "We couldn't finish this right now. Please try again later.",
                        'es' => 'No fue posible completarlo ahora. Inténtalo más tarde.',
                        'zh' => '暂时无法完成,请稍后再试。'],
        'confirm_q' => ['pt' => 'Confirmar a inscrição na newsletter?', 'en' => 'Confirm your newsletter subscription?',
                        'es' => '¿Confirmar la suscripción al boletín?', 'zh' => '确认订阅简报?'],
        'confirm_b' => ['pt' => 'Confirmar inscrição', 'en' => 'Confirm subscription',
                        'es' => 'Confirmar suscripción', 'zh' => '确认订阅'],
        'confirmed' => ['pt' => 'Inscrição confirmada. Até amanhã!', 'en' => 'Subscription confirmed. See you tomorrow!',
                        'es' => 'Suscripción confirmada. ¡Hasta mañana!', 'zh' => '订阅已确认。明天见!'],
        'leave_q' => ['pt' => 'Cancelar a inscrição na newsletter?', 'en' => 'Unsubscribe from the newsletter?',
                      'es' => '¿Cancelar la suscripción al boletín?', 'zh' => '退订简报?'],
        'leave_b' => ['pt' => 'Cancelar inscrição', 'en' => 'Unsubscribe', 'es' => 'Cancelar suscripción',
                      'zh' => '退订'],
        'left' => ['pt' => 'Pronto: você não vai receber mais a newsletter, e o seu e-mail foi apagado.',
                   'en' => "Done: you won't receive the newsletter anymore, and your e-mail address was deleted.",
                   'es' => 'Listo: ya no recibirás el boletín y tu correo fue borrado.',
                   'zh' => '已完成:您将不再收到简报,您的邮箱地址已被删除。'],
        'invalid' => ['pt' => 'Este link não vale mais. Se quiser assinar, faça o cadastro de novo.',
                      'en' => 'This link is no longer valid. To subscribe, sign up again.',
                      'es' => 'Este enlace ya no es válido. Para suscribirte, regístrate de nuevo.',
                      'zh' => '此链接已失效。如需订阅,请重新注册。'],
        'back' => ['pt' => 'Voltar ao blog', 'en' => 'Back to the blog', 'es' => 'Volver al blog', 'zh' => '返回博客'],
    ];
    return $texts[$key][$lang] ?? $texts[$key]['pt'] ?? $key;
}

function sb_nl_slug(string $engine_or_slug = ''): string
{
    $flip = array_flip(SB_NL_LANGS);
    if (isset($flip[$engine_or_slug])) {
        return $flip[$engine_or_slug];
    }
    if (isset(SB_NL_LANGS[$engine_or_slug])) {
        return $engine_or_slug;
    }
    $current = function_exists('pll_current_language') ? (string) pll_current_language('slug') : 'pt';
    return isset(SB_NL_LANGS[$current]) ? $current : 'pt';
}

function sb_nl_configured(): bool
{
    return defined('SB_NEWSLETTER_API') && SB_NEWSLETTER_API && defined('SB_NEWSLETTER_TOKEN') && SB_NEWSLETTER_TOKEN;
}

/** Chamada ao motor (`/api/…`, também usada pelo sb-pauta.php). JSON da resposta, ou null. */
function sb_nl_call(string $path, ?array $body = null, int $timeout = 8): ?array
{
    if (!sb_nl_configured()) {
        return null;
    }
    $args = ['timeout' => $timeout,
             'headers' => ['Authorization' => 'Bearer ' . SB_NEWSLETTER_TOKEN,
                           'Content-Type' => 'application/json']];
    $url = rtrim((string) SB_NEWSLETTER_API, '/') . '/api/' . ltrim($path, '/');
    $response = $body === null ? wp_remote_get($url, $args)
        : wp_remote_post($url, $args + ['body' => wp_json_encode($body)]);
    if (is_wp_error($response)) {
        return null;
    }
    $code = (int) wp_remote_retrieve_response_code($response);
    $data = json_decode((string) wp_remote_retrieve_body($response), true);
    return ($code >= 200 && $code < 300 && is_array($data)) ? $data : null;
}

/** O cadastro está aberto? (pergunta ao motor, guarda 5 minutos) */
function sb_nl_open(): bool
{
    if (!sb_nl_configured()) {
        return false;
    }
    $cached = get_transient('sb_nl_open');
    if ($cached !== false) {
        return $cached === 'yes';
    }
    $status = sb_nl_call('newsletter/status');
    $open = is_array($status) && !empty($status['open']);
    set_transient('sb_nl_open', $open ? 'yes' : 'no', 300);
    return $open;
}

function sb_nl_privacy_url(string $slug): string
{
    // O permalink quase nunca muda: um transient poupa a consulta com meta em cada render.
    $url = get_transient("sb_nl_privacy_$slug");
    if (!is_string($url) || $url === '') {
        $pages = get_posts(['post_type' => 'page', 'post_status' => 'publish', 'numberposts' => 1,
                            'meta_key' => '_sb_page', 'meta_value' => 'privacidade', 'lang' => $slug,
                            'suppress_filters' => false, 'fields' => 'ids']);
        $url = $pages ? (string) get_permalink($pages[0]) : home_url('/');
        set_transient("sb_nl_privacy_$slug", $url, 12 * HOUR_IN_SECONDS);
    }
    return $url;
}

/** O formulário (rodapé do tema e atalho `[sb_newsletter]`). Vazio se o cadastro estiver fechado. */
function sb_newsletter_form(): string
{
    if (!sb_nl_open()) {
        return '';
    }
    $slug = sb_nl_slug();
    $action = esc_url(rest_url('sb/v1/newsletter/subscribe'));
    return '<section class="sb-nl" aria-labelledby="sb-nl-t"><div class="in">'
        . '<h2 id="sb-nl-t">' . esc_html(sb_nl_t('title', $slug)) . '</h2>'
        . '<p>' . esc_html(sb_nl_t('pitch', $slug)) . '</p>'
        . '<form method="post" action="' . $action . '">'
        . '<input type="hidden" name="lang" value="' . esc_attr($slug) . '">'
        // Campo isca: escondido de pessoas, os robôs de spam preenchem.
        . '<p class="sb-nl-hp" aria-hidden="true"><label>Website <input type="text" name="website" tabindex="-1" autocomplete="off"></label></p>'
        . '<label class="sb-nl-l" for="sb-nl-e">' . esc_html(sb_nl_t('email', $slug)) . '</label>'
        . '<input id="sb-nl-e" type="email" name="email" required maxlength="254" autocomplete="email" placeholder="' . esc_attr(sb_nl_t('email', $slug)) . '">'
        . '<button type="submit">' . esc_html(sb_nl_t('button', $slug)) . '</button>'
        . '<p class="sb-nl-c">' . esc_html(sb_nl_t('consent', $slug)) . ' <a href="' . esc_url(sb_nl_privacy_url($slug)) . '">'
        . esc_html(sb_nl_t('privacy', $slug)) . '</a>. ' . esc_html(sb_nl_t('leave_any', $slug)) . '</p>'
        . '</form></div></section>';
}
add_shortcode('sb_newsletter', static fn(): string => sb_newsletter_form());

/** Página simples (fora do tema: é servida pela REST) e fim do pedido. */
function sb_nl_page(string $slug, string $message, string $form = '', int $status = 200): void
{
    status_header($status);
    nocache_headers();
    header('Content-Type: text/html; charset=utf-8');
    header('Referrer-Policy: no-referrer');          // o token está no endereço
    header('X-Content-Type-Options: nosniff');
    header('X-Robots-Tag: noindex, nofollow');
    header("Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'");
    $home = esc_url(function_exists('pll_home_url') ? pll_home_url($slug) : home_url('/'));
    $name = esc_html(get_bloginfo('name'));
    $lang = esc_attr(SB_NL_LANGS[$slug] ?? 'pt-BR');
    echo "<!doctype html><html lang=\"$lang\"><head><meta charset=\"utf-8\">"
        . '<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">'
        . "<title>$name</title><style>body{font:17px/1.6 system-ui,-apple-system,Segoe UI,sans-serif;"
        . 'background:#f3f4f7;color:#15171c;margin:0;padding:40px 16px}main{max-width:520px;margin:0 auto;'
        . 'background:#fff;border-radius:12px;padding:28px;box-shadow:0 8px 24px rgb(16 24 40/.08)}'
        . 'button{font:inherit;padding:10px 18px;border:0;border-radius:8px;background:#2457d6;color:#fff;cursor:pointer}'
        . "a{color:#2457d6}</style></head><body><main><p><strong>$name</strong></p>"
        . '<p>' . esc_html($message) . "</p>$form<p><a href=\"$home\">" . esc_html(sb_nl_t('back', $slug))
        . '</a></p></main></body></html>';
    exit;
}

/** Formulário de um botão, que reenvia o token por POST para o mesmo endereço. */
function sb_nl_button(string $route, string $token, string $label): string
{
    return '<form method="post" action="' . esc_url(rest_url("sb/v1/newsletter/$route")) . '">'
        . '<input type="hidden" name="t" value="' . esc_attr($token) . '">'
        . '<button type="submit">' . esc_html($label) . '</button></form>';
}

function sb_nl_link_route(WP_REST_Request $request, string $action): void
{
    $token = substr((string) $request->get_param('t'), 0, 200);
    $slug = sb_nl_slug((string) $request->get_param('l'));
    $post = $request->get_method() === 'POST';
    $one_click = $post && trim((string) $request->get_param('List-Unsubscribe')) === 'One-Click';
    $state = sb_nl_call(($post ? "newsletter/$action" : "newsletter/check/$action"),
                        ['token' => $token]);
    if ($one_click) {                                  // cliente de e-mail: sem página
        status_header($state && ($state['state'] ?? '') === 'done' ? 200 : 400);
        header('Content-Type: text/plain; charset=utf-8');
        echo $state && ($state['state'] ?? '') === 'done' ? 'ok' : 'invalid';
        exit;
    }
    if ($state === null) {
        sb_nl_page($slug, sb_nl_t('try_later', $slug), '', 503);
    }
    $slug = sb_nl_slug((string) ($state['language'] ?? $slug));
    $current = (string) ($state['state'] ?? 'invalid');
    if ($current === 'invalid') {
        sb_nl_page($slug, sb_nl_t('invalid', $slug), '', 404);
    }
    if ($current === 'done') {
        sb_nl_page($slug, sb_nl_t($action === 'confirm' ? 'confirmed' : 'left', $slug));
    }
    sb_nl_page($slug, sb_nl_t($action === 'confirm' ? 'confirm_q' : 'leave_q', $slug),
        sb_nl_button($action, $token, sb_nl_t($action === 'confirm' ? 'confirm_b' : 'leave_b', $slug)));
}

add_action('rest_api_init', static function (): void {
    register_rest_route('sb/v1', '/newsletter/subscribe', [
        'methods' => 'POST',
        'permission_callback' => '__return_true',
        'callback' => static function (WP_REST_Request $request): void {
            $slug = sb_nl_slug((string) $request->get_param('lang'));
            $ip = function_exists('sb_login_ip') ? sb_login_ip() : (string) ($_SERVER['REMOTE_ADDR'] ?? '');
            $result = sb_nl_call('newsletter/subscribe', [
                'email' => substr((string) $request->get_param('email'), 0, 320),
                'language' => SB_NL_LANGS[$slug],
                'site_key' => defined('SB_SITE_KEY') ? SB_SITE_KEY : '',
                'ip' => substr($ip, 0, 64),
                'honeypot' => substr((string) $request->get_param('website'), 0, 200),
            ]);
            if ($result === null || ($result['error'] ?? '') === 'fechado' || ($result['error'] ?? '') === 'limite') {
                sb_nl_page($slug, sb_nl_t('try_later', $slug), '', 503);
            }
            if (empty($result['ok'])) {
                sb_nl_page($slug, sb_nl_t('bad_email', $slug), '', 400);
            }
            sb_nl_page($slug, sb_nl_t('check_mail', $slug));
        },
    ]);
    foreach (['confirm', 'unsubscribe'] as $action) {
        register_rest_route('sb/v1', "/newsletter/$action", [
            'methods' => ['GET', 'POST'],
            'permission_callback' => '__return_true',
            'callback' => static function (WP_REST_Request $request) use ($action): void {
                sb_nl_link_route($request, $action);
            },
        ]);
    }
    // Webhook do Brevo (bounces e reclamações). A chave vai no endereço; quem a confere é o motor.
    register_rest_route('sb/v1', '/newsletter/brevo', [
        'methods' => 'POST',
        'permission_callback' => '__return_true',
        'callback' => static function (WP_REST_Request $request): WP_REST_Response {
            $events = $request->get_json_params();
            $events = isset($events['event']) ? [$events] : (is_array($events) ? $events : []);
            $done = 0;
            foreach (array_slice($events, 0, 100) as $event) {
                if (!is_array($event)) {
                    continue;
                }
                $result = sb_nl_call('newsletter/brevo-event', [
                    'key' => substr((string) $request->get_param('k'), 0, 200),
                    'event' => substr((string) ($event['event'] ?? ''), 0, 64),
                    'email' => substr((string) ($event['email'] ?? ''), 0, 320),
                ]);
                if ($result === null) {
                    return new WP_REST_Response(['ok' => false], 503);   // o Brevo tenta de novo
                }
                $done++;
            }
            return new WP_REST_Response(['ok' => true, 'events' => $done], 200);
        },
    ]);
});
