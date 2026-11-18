<?php
/**
 * Credencial do motor no blog com uma senha CONHECIDA (E19.4, Helm chart).
 *
 *   wp eval-file /setup/app-password.php
 *
 * Variáveis: SB_WP_APP_USER (padrão sb-publisher), SB_WP_APP_PASSWORD (24 letras/dígitos, gerada
 * pelo chart e guardada no Secret que o motor também lê), SB_SITE_HOST.
 *
 * Diferente do `wp user application-password create` (senha aleatória que alguém tem de copiar),
 * aqui o hash da senha do Secret é gravado no usuário: o motor e o blog nascem combinados, sem
 * acesso à API do Kubernetes. Pode repetir-se: se a senha «motor» já confere, nada muda; se o
 * Secret mudou, a antiga é trocada.
 */

$user_login = getenv( 'SB_WP_APP_USER' ) ?: 'sb-publisher';
$password   = (string) getenv( 'SB_WP_APP_PASSWORD' );
$host       = (string) getenv( 'SB_SITE_HOST' );

if ( ! preg_match( '/^[A-Za-z0-9]{24}$/', $password ) ) {
	WP_CLI::error( 'SB_WP_APP_PASSWORD tem de ter 24 letras/dígitos' );
}

$user = get_user_by( 'login', $user_login );
if ( ! $user ) {
	$id = wp_insert_user(
		array(
			'user_login'   => $user_login,
			'user_email'   => 'publisher@' . ( $host ?: 'localhost' ),
			'user_pass'    => wp_generate_password( 32, true, true ),
			'role'         => 'author',
			'display_name' => 'Redação',
		)
	);
	if ( is_wp_error( $id ) ) {
		WP_CLI::error( 'não criou o usuário ' . $user_login . ': ' . $id->get_error_message() );
	}
	$user = get_user_by( 'id', $id );
	WP_CLI::log( 'usuário ' . $user_login . ' criado' );
}

$items = WP_Application_Passwords::get_user_application_passwords( $user->ID );
$keep  = array();
foreach ( $items as $item ) {
	if ( 'motor' !== $item['name'] ) {
		$keep[] = $item;
		continue;
	}
	if ( WP_Application_Passwords::check_password( $password, $item['password'] ) ) {
		WP_CLI::success( 'credencial do motor já confere: mantida' );
		return;
	}
}

$keep[] = array(
	'uuid'      => wp_generate_uuid4(),
	'app_id'    => '',
	'name'      => 'motor',
	'password'  => WP_Application_Passwords::hash_password( $password ),
	'created'   => time(),
	'last_used' => null,
	'last_ip'   => null,
);
update_user_meta( $user->ID, WP_Application_Passwords::USERMETA_KEY_APPLICATION_PASSWORDS, $keep );
update_network_option( get_main_network_id(), WP_Application_Passwords::OPTION_KEY_IN_USE, true );
WP_CLI::success( 'credencial do motor gravada' );
