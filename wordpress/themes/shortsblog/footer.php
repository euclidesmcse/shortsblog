<?php if (!defined('ABSPATH')) { exit; } ?>
</main>
<?php if (function_exists('sb_newsletter_form')) { echo sb_newsletter_form(); } // E17: vazio enquanto fechada ?>
<footer class="sb-foot">
  <div class="in">
    <span class="sb-brand">© <?php echo esc_html(wp_date('Y')); ?> <?php bloginfo('name'); ?> · ShortsBlog</span>
    <?php wp_nav_menu(['theme_location' => 'footer', 'container' => false, 'depth' => 1,
                       'fallback_cb' => 'sb_footer_links']); ?>
    <p class="sb-credit"><?php echo esc_html(sb_t('credit')); ?>
      <a href="https://cloudarquiteto.com" rel="author external noopener" target="_blank"><?php
        echo esc_html(sb_t('credit_cta')); ?></a></p>
  </div>
</footer>
<?php wp_footer(); ?>
</body>
</html>
