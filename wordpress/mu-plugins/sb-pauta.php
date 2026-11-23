<?php
/**
 * Plugin Name: ShortsBlog — Pauta por e-mail
 * Description: Webhook de entrada do Brevo (D-094, opção A da D-OPEN-15): newsletters de
 *              fabricantes chegam como JSON e cada item vira uma ideia «só pauta» no motor.
 *              O blog só repassa (remetente, assunto, HTML, Authentication-Results) pela rede interna, com
 *              o token WordPress↔motor da newsletter; quem valida o remetente, a chave do
 *              webhook e extrai os itens é o motor.
 *
 * A chave do Brevo vai no cabeçalho `X-SB-Pauta-Key` (R61: no endereço, `?k=…`, ficava em
 * claro nos logs de acesso) e quem a confere é o motor (`pauta_webhook_token`). O `?k=` ainda
 * vale durante a troca; o motor conta por onde a chave chegou (`key_via`) para se saber
 * quando já se pode tirá-lo do endereço do webhook. Sem `SB_NEWSLETTER_API`/`SB_NEWSLETTER_TOKEN`, a rota
 * responde 503 e o Brevo tenta de novo. E-mail é entrada não confiável: nada daqui vira
 * instrução; o conteúdo segue ao motor como dado, truncado.
 */

if (!defined('ABSPATH')) {
    exit;
}

/** POST ao motor (`/api/pauta/…`): a mesma chamada do sb-newsletter.php, com mais fôlego. */
function sb_pauta_call(string $path, array $body): ?array
{
    return sb_nl_call('pauta/' . ltrim($path, '/'), $body, 15);
}

/** Os valores de `Authentication-Results` do e-mail, inteiros e um a um (quem decide o que
 *  vale — authserv-id do Brevo, `pass`, alinhamento com o From — é o motor, R62). */
function sb_pauta_auth_headers(array $item): array
{
    $headers = is_array($item['Headers'] ?? null) ? $item['Headers'] : [];
    $out = [];
    foreach ($headers as $name => $value) {
        if (strtolower((string) $name) !== 'authentication-results') {
            continue;
        }
        foreach (is_array($value) ? $value : [$value] as $one) {
            $out[] = substr((string) $one, 0, 2000);
            if (count($out) >= 20) {
                return $out;
            }
        }
    }
    return $out;
}

/** A chave do webhook: cabeçalho primeiro (R61), endereço só durante a troca. */
function sb_pauta_key(WP_REST_Request $request): array
{
    $header = trim((string) $request->get_header('x_sb_pauta_key'));
    if ($header !== '') {
        return [substr($header, 0, 200), 'header'];
    }
    $query = (string) $request->get_param('k');
    return [substr($query, 0, 200), $query !== '' ? 'query' : ''];
}

add_action('rest_api_init', static function (): void {
    register_rest_route('sb/v1', '/pauta/brevo', [
        'methods' => 'POST',
        'permission_callback' => '__return_true',
        'callback' => static function (WP_REST_Request $request): WP_REST_Response {
            if (!function_exists('sb_nl_configured') || !sb_nl_configured()) {
                return new WP_REST_Response(['ok' => false], 503);
            }
            $payload = $request->get_json_params();
            $items = is_array($payload['items'] ?? null) ? $payload['items']
                : (is_array($payload['Items'] ?? null) ? $payload['Items'] : []);
            [$key, $via] = sb_pauta_key($request);
            $done = 0;
            foreach (array_slice($items, 0, 10) as $item) {
                if (!is_array($item)) {
                    continue;
                }
                $from = $item['From'] ?? [];
                $sender = is_array($from) ? (string) ($from['Address'] ?? '') : (string) $from;
                $result = sb_pauta_call('brevo-inbound', [
                    'key' => $key,
                    'key_via' => $via,
                    'sender' => substr($sender, 0, 320),
                    'subject' => substr((string) ($item['Subject'] ?? ''), 0, 500),
                    'html' => substr((string) ($item['RawHtmlBody'] ?? ''), 0, 1000000),
                    'auth' => sb_pauta_auth_headers($item),
                ]);
                if ($result === null) {
                    return new WP_REST_Response(['ok' => false], 503);   // o Brevo tenta de novo
                }
                $done++;
            }
            return new WP_REST_Response(['ok' => true, 'emails' => $done], 200);
        },
    ]);
});
