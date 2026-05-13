/**
 * ECharts option presets shared across the cross-experiment pages.
 *
 * Keeps colour, font, and grid spacing consistent so the four pages feel
 * like the same product. Each helper returns a partial `EChartsCoreOption`
 * the page composes with its data-specific bits.
 */

import type { EChartsCoreOption } from 'echarts/core';

/** Same palette the rest of the UI uses (slate + amber + emerald + rose). */
export const PALETTE = [
  '#0f172a', // slate-900
  '#f59e0b', // amber-500
  '#16a34a', // green-600
  '#dc2626', // red-600
  '#0284c7', // sky-600
  '#9333ea', // purple-600
  '#0d9488', // teal-600
  '#ea580c', // orange-600
] as const;

const SHARED_TEXT_STYLE = {
  fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
  color: '#0f172a',
};

/** Tooltip preset shared by every chart — dark background, light text. */
export const tooltipPreset = {
  backgroundColor: '#0f172a',
  borderColor: '#0f172a',
  textStyle: { color: '#f8fafc', fontSize: 12 },
};

/** Sensible defaults for any chart in the app. Spread it into `options`. */
export const baseOptions: EChartsCoreOption = {
  color: [...PALETTE],
  textStyle: SHARED_TEXT_STYLE,
  grid: { left: 48, right: 16, top: 32, bottom: 32, containLabel: true },
  tooltip: { trigger: 'item', ...tooltipPreset },
};
