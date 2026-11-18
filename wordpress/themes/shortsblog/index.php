<?php
/** Página inicial, categorias, etiquetas e pesquisa: destaque + grade de cartões. */
if (!defined('ABSPATH')) { exit; }
get_header();
global $wp_query;
$home = !is_paged() && is_home();
?>
<?php if (is_archive() || is_search()) : ?>
  <h1 class="sb-title"><?php echo is_search() ? esc_html(get_search_query()) : wp_kses_post(get_the_archive_title()); ?></h1>
<?php endif; ?>
<?php if (have_posts()) : $i = 0; ?>
  <?php while (have_posts()) : the_post(); $i++;
    if ($home && $i === 1) : ?>
  <article <?php post_class('sb-hero'); ?>>
    <a class="sb-hero-link" href="<?php the_permalink(); ?>" tabindex="-1" aria-hidden="true"><?php echo sb_media('sb-hero'); // phpcs:ignore ?></a>
    <div class="sb-hero-body">
      <span class="sb-badge"><?php echo esc_html(sb_t('featured')); ?></span>
      <?php echo sb_category_link(); // phpcs:ignore -- já escapado ?>
      <h2><a href="<?php the_permalink(); ?>"><?php the_title(); ?></a></h2>
      <p><?php echo esc_html(get_the_excerpt()); ?></p>
      <span class="sb-meta"><time datetime="<?php echo esc_attr(get_the_date('c')); ?>"><?php echo esc_html(sb_date()); ?></time> · <?php echo esc_html(sb_reading_minutes()); ?> min</span>
      <a class="sb-btn" href="<?php the_permalink(); ?>"><?php echo esc_html(sb_t('read')); ?> →</a>
    </div>
  </article>
  <?php if ($wp_query->post_count > 1) : ?><h2 class="sb-section"><?php echo esc_html(sb_t('latest')); ?></h2><?php endif; ?>
  <div class="sb-grid">
    <?php else :
      if ($i === 1) { echo '<div class="sb-grid">'; } ?>
  <article <?php post_class('sb-card'); ?>>
    <a class="sb-card-media" href="<?php the_permalink(); ?>" tabindex="-1" aria-hidden="true"><?php echo sb_media(); // phpcs:ignore ?></a>
    <div class="sb-card-body">
      <?php echo sb_category_link(); // phpcs:ignore -- já escapado ?>
      <h2><a href="<?php the_permalink(); ?>"><?php the_title(); ?></a></h2>
      <p><?php echo esc_html(get_the_excerpt()); ?></p>
      <span class="sb-meta"><time datetime="<?php echo esc_attr(get_the_date('c')); ?>"><?php echo esc_html(sb_date()); ?></time> · <?php echo esc_html(sb_reading_minutes()); ?> min</span>
    </div>
  </article>
    <?php endif; endwhile; ?>
  </div>
<?php else : ?>
  <p class="sb-empty"><?php echo esc_html(sb_t('empty')); ?></p>
<?php endif; ?>
<nav class="sb-pages"><span><?php previous_posts_link(); ?></span><span><?php next_posts_link(); ?></span></nav>
<?php get_footer();
