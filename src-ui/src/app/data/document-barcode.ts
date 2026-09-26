export interface DocumentBarcode {
  page: number

  value: string

  format: string

  // [x0, y0, x1, y1] in PDF coordinates of the page as shown
  rect?: number[]
}
