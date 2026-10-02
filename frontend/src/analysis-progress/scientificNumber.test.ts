import { describe, expect, it } from 'vitest'
import { formatScientificNumber } from './scientificNumber'

describe('scientific number presentation', () => {
  it.each([
    [4.811026573181152, '4.81'],
    [0.8158712983131409, '0.82'],
    [105.03999999999999, '105.04'],
    [1.2, '1.2'],
    [-0.0001, '-1.0e-4'],
    [0, '0'],
    [50, '50'],
    [0.00036742, '3.7e-4'],
  ])('formats %s without exposing false precision', (value, expected) => {
    expect(formatScientificNumber(value)).toBe(expected)
  })
})
