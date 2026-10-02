/** 공개 랜딩과 작업공간에서 함께 쓰는 EviDrug 브랜드 링크다. */
export function Brand() {
  return (
    <a className="brand" href="/" aria-label="EviDrug 홈">
      <span className="brand__mark" aria-hidden="true">
        <span />
        <span />
        <span />
      </span>
      <span>EviDrug</span>
    </a>
  )
}
