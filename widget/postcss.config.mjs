import tailwindcss from '@tailwindcss/postcss';

/**
 * The widget lives inside arbitrary storefronts. `rem` is relative to the HOST
 * page's <html> font-size (e.g. 62.5% on many sites), which would shrink or
 * blow up the widget. Converting rem → px after Tailwind runs makes sizing
 * independent of the host page.
 */
const REM_PX = 16;
const REM_PATTERN = /(-?\d*\.?\d+)rem\b/g;
const toPx = (value) =>
  value.replace(REM_PATTERN, (_, n) => `${Number.parseFloat(n) * REM_PX}px`);

const remToPx = () => ({
  postcssPlugin: 'widget-rem-to-px',
  OnceExit(root) {
    root.walkDecls((decl) => {
      if (decl.value.includes('rem')) decl.value = toPx(decl.value);
    });
    root.walkAtRules((rule) => {
      if (rule.params.includes('rem')) rule.params = toPx(rule.params);
    });
  },
});
remToPx.postcss = true;

export default {
  plugins: [tailwindcss(), remToPx()],
};
