/// <reference types="node" />

import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const css = readFileSync(resolve(process.cwd(), 'src/index.css'), 'utf8')

function color(name: string): string {
  const value = css.match(new RegExp(`--${name}:\\s*(#[0-9a-fA-F]{6})\\s*;`))?.[1]
  if (!value) throw new Error(`Missing hex color token: ${name}`)
  return value
}

function luminance(value: string): number {
  const channels = value.slice(1).match(/.{2}/g)
  if (!channels) throw new Error(`Invalid color: ${value}`)
  const [red, green, blue] = channels.map((channel) => {
    const srgb = Number.parseInt(channel, 16) / 255
    return srgb <= 0.04045 ? srgb / 12.92 : ((srgb + 0.055) / 1.055) ** 2.4
  })
  return 0.2126 * red + 0.7152 * green + 0.0722 * blue
}

function contrast(foreground: string, background: string): number {
  const first = luminance(color(foreground))
  const second = luminance(color(background))
  return (Math.max(first, second) + 0.05) / (Math.min(first, second) + 0.05)
}

describe('color palette contrast', () => {
  it.each([
    ['color-brand-action', 'color-text-inverse'],
    ['color-text-primary', 'color-surface-canvas'],
    ['color-text-secondary', 'color-surface-canvas'],
    ['color-text-tertiary', 'color-surface-canvas'],
    ['color-status-success', 'color-status-success-soft'],
    ['color-status-warning', 'color-status-warning-soft'],
    ['color-status-danger', 'color-status-danger-soft'],
    ['color-status-information', 'color-status-information-soft'],
    ['color-text-inverse', 'color-surface-inverse'],
  ])('%s on %s meets normal-text contrast', (foreground, background) => {
    expect(contrast(foreground, background)).toBeGreaterThanOrEqual(4.5)
  })

  it('keeps strong control borders distinguishable on the canvas', () => {
    expect(contrast('color-border-strong', 'color-surface-canvas')).toBeGreaterThanOrEqual(3)
  })
})
