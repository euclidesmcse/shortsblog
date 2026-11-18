<?php if (!defined('ABSPATH')) { exit; } ?>
<!doctype html>
<html <?php language_attributes(); ?>>
<head>
<meta charset="<?php bloginfo('charset'); ?>">
<meta name="viewport" content="width=device-width, initial-scale=1">
<?php wp_head(); ?>
</head>
<body <?php body_class(); ?>>
<?php wp_body_open(); ?>
<a class="sb-skip" href="#conteudo"><?php echo esc_html(sb_t('skip')); ?></a>
<header class="sb-top">
  <div class="in">
    <a class="sb-name" href="<?php echo esc_url(home_url('/')); ?>"><?php bloginfo('name'); ?></a>
    <form class="sb-search" role="search" method="get" action="<?php echo esc_url(home_url('/')); ?>">
      <input type="search" name="s" value="<?php echo esc_attr(get_search_query()); ?>" placeholder="<?php echo esc_attr(sb_t('search')); ?>…" aria-label="<?php echo esc_attr(sb_t('search')); ?>">
    </form>
    <?php if (function_exists('pll_the_languages')) : ?>
      <ul class="sb-langs" aria-label="Idioma / Language">
        <?php pll_the_languages(['display_names_as' => 'slug', 'hide_if_no_translation' => 0,
                           'hide_if_empty' => 0]); ?>
      </ul>
    <?php endif; ?>
  </div>
  <nav class="sb-nav" aria-label="Categorias">
    <ul class="in">
      <li class="<?php echo is_home() ? 'current-cat' : ''; ?>"><a href="<?php echo esc_url(home_url('/')); ?>"><?php echo esc_html(sb_t('all')); ?></a></li>
      <?php wp_list_categories(['title_li' => '', 'hide_empty' => true, 'depth' => 1,
                                'orderby' => 'count', 'order' => 'DESC', 'number' => 8]); ?>
    </ul>
  </nav>
</header>
<main class="sb-main" id="conteudo">
