<?php
/** Um artigo ou uma página. */
if (!defined('ABSPATH')) { exit; }
get_header();
while (have_posts()) : the_post(); ?>
<article <?php post_class('sb-article'); ?>>
  <header class="sb-cover">
    <div class="sb-cover-in">
      <?php if (is_single()) { echo sb_category_link(); } // phpcs:ignore -- já escapado ?>
      <h1><?php the_title(); ?></h1>
      <?php if (is_single() && has_excerpt()) : ?><p class="sb-excerpt"><?php echo esc_html(get_the_excerpt()); ?></p><?php endif; ?>
      <?php if (is_single()) : ?>
        <span class="sb-meta"><time datetime="<?php echo esc_attr(get_the_date('c')); ?>"><?php echo esc_html(sb_date()); ?></time> · <?php echo esc_html(sb_reading_minutes()); ?> min</span>
      <?php endif; ?>
    </div>
    <?php if (is_single() && has_post_thumbnail()) : ?><figure class="sb-cover-img"><?php the_post_thumbnail('sb-hero'); ?>
      <?php if (get_the_post_thumbnail_caption()) : ?><figcaption><?php echo esc_html(get_the_post_thumbnail_caption()); ?></figcaption><?php endif; ?></figure><?php endif; ?>
  </header>
  <div class="sb-content"><?php the_content(); ?></div>
</article>
<?php if (is_single()) : $sb_related = sb_related_posts(); ?>
<?php if ($sb_related) : ?>
<aside class="sb-related" aria-label="<?php echo esc_attr(sb_t('related')); ?>">
  <h2 class="sb-section"><?php echo esc_html(sb_t('related')); ?></h2>
  <ul>
    <?php foreach ($sb_related as $sb_rp) : ?>
    <li><a href="<?php echo esc_url(get_permalink($sb_rp)); ?>"><?php echo esc_html(get_the_title($sb_rp)); ?></a>
      <span class="sb-meta"><?php echo esc_html(get_the_date('', $sb_rp)); ?></span></li>
    <?php endforeach; ?>
  </ul>
</aside>
<?php endif; endif; ?>
<?php endwhile;
get_footer();
