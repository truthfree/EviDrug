const DEFAULT_DECIMAL_PLACES = 2
const SCIENTIFIC_NOTATION_THRESHOLD = 0.005

/**
 * 독자용 숫자는 의미 없는 부동소수점 자릿수를 숨긴다.
 * 계산과 판정에는 이 문자열이 아니라 API의 원시 number를 계속 사용한다.
 */
export function formatScientificNumber(value: number): string {
  if (!Number.isFinite(value)) throw new TypeError('value must be finite')
  if (Number.isInteger(value)) return String(value)
  if (value !== 0 && Math.abs(value) < SCIENTIFIC_NOTATION_THRESHOLD) {
    return value.toExponential(1)
  }
  const rendered = value.toFixed(DEFAULT_DECIMAL_PLACES).replace(/\.?0+$/, '')
  return rendered === '-0' ? '0' : rendered
}
