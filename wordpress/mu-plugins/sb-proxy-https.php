<?php
/**
 * Plugin Name: ShortsBlog — HTTPS atrás do proxy
 * Description: O Traefik termina o TLS e fala HTTP com o WordPress. Sem isto o WordPress
 *              julga que o site não é HTTPS e desativa as Application Passwords (R6).
 */

if (!defined('ABSPATH')) {
    exit;
}

// Só o Traefik chega a este container (rede `edge`); o cabeçalho vem dele.
if (isset($_SERVER['HTTP_X_FORWARDED_PROTO'])
    && strtolower((string) $_SERVER['HTTP_X_FORWARDED_PROTO']) === 'https') {
    $_SERVER['HTTPS'] = 'on';
    $_SERVER['SERVER_PORT'] = 443;
}
