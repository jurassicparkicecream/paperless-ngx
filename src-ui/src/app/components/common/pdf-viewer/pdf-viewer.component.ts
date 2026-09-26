import { Clipboard } from '@angular/cdk/clipboard'
import {
  AfterViewInit,
  Component,
  DOCUMENT,
  ElementRef,
  EventEmitter,
  inject,
  Input,
  OnChanges,
  OnDestroy,
  Output,
  SimpleChanges,
  ViewChild,
} from '@angular/core'
import {
  AnnotationMode,
  getDocument,
  GlobalWorkerOptions,
  PDFDocumentLoadingTask,
  PDFDocumentProxy,
} from 'pdfjs-dist/legacy/build/pdf.mjs'
import {
  EventBus,
  LinkTarget,
  PDFFindController,
  PDFLinkService,
  PDFSinglePageViewer,
  PDFViewer,
} from 'pdfjs-dist/web/pdf_viewer.mjs'
import { DocumentBarcode } from 'src/app/data/document-barcode'
import {
  PdfRenderMode,
  PdfZoomLevel,
  PdfZoomScale,
  PngxPdfDocumentProxy,
} from './pdf-viewer.types'

@Component({
  selector: 'pngx-pdf-viewer',
  templateUrl: './pdf-viewer.component.html',
  styleUrl: './pdf-viewer.component.scss',
})
export class PngxPdfViewerComponent
  implements AfterViewInit, OnChanges, OnDestroy
{
  private readonly document = inject<Document>(DOCUMENT)

  @Input() src!: string
  @Input() sourceRevision = 0
  @Input() password?: string
  @Input() page?: number
  @Output() pageChange = new EventEmitter<number>()
  @Input() rotation?: number
  @Input() renderMode: PdfRenderMode = PdfRenderMode.All
  @Input() selectable = true
  @Input() searchQuery = ''
  @Input() zoom: PdfZoomLevel = PdfZoomLevel.One
  @Input() zoomScale: PdfZoomScale = PdfZoomScale.PageWidth
  // Barcodes with their position, shown with a copy button on hover
  @Input() barcodes: DocumentBarcode[] = []

  @Output() afterLoadComplete = new EventEmitter<PngxPdfDocumentProxy>()
  @Output() rendered = new EventEmitter<void>()
  @Output() loadError = new EventEmitter<unknown>()

  @ViewChild('container', { static: true })
  private readonly container!: ElementRef<HTMLDivElement>

  @ViewChild('viewer', { static: true })
  private readonly viewer!: ElementRef<HTMLDivElement>

  private hasLoaded = false
  private loadingTask?: PDFDocumentLoadingTask
  private resizeObserver?: ResizeObserver
  private pdf?: PDFDocumentProxy
  private pdfViewer?: PDFViewer | PDFSinglePageViewer
  private hasRenderedPage = false
  private lastFindQuery = ''
  private lastViewerPage?: number

  private readonly eventBus = new EventBus()
  private readonly linkService = new PDFLinkService({
    eventBus: this.eventBus,
    externalLinkTarget: LinkTarget.BLANK,
    externalLinkRel: 'noopener noreferrer nofollow',
  })
  private readonly findController = new PDFFindController({
    eventBus: this.eventBus,
    linkService: this.linkService,
    updateMatchesCountOnProgress: false,
  })

  private readonly clipboard = inject(Clipboard)
  private readonly copyTimeouts = new Set<ReturnType<typeof setTimeout>>()

  private readonly onPageRendered = (evt?: { source?: BarcodePageView }) => {
    this.renderBarcodeLayer(evt?.source)
    this.hasRenderedPage = true
    this.dispatchFindIfReady()
    this.rendered.emit()
  }
  private readonly onPagesInit = () => this.applyViewerState()
  private readonly onPageChanging = (evt: { pageNumber: number }) => {
    // Avoid [(page)] two-way binding re-triggers navigation
    this.lastViewerPage = evt.pageNumber
    this.pageChange.emit(evt.pageNumber)
  }

  ngOnChanges(changes: SimpleChanges): void {
    if (changes['src'] || changes['sourceRevision'] || changes['password']) {
      this.resetViewerState()
      if (this.src) {
        this.loadDocument()
      }
      return
    }

    if (changes['zoomScale']) {
      this.setupResizeObserver()
    }

    if (changes['selectable'] || changes['renderMode']) {
      this.initViewer()
    }

    if (
      changes['page'] ||
      changes['zoom'] ||
      changes['zoomScale'] ||
      changes['rotation']
    ) {
      // Prevent loop with page / scale application see https://github.com/paperless-ngx/paperless-ngx/issues/13404
      this.applyViewerState(
        !!(changes['zoom'] || changes['zoomScale'] || changes['rotation'])
      )
    }

    if (changes['searchQuery']) {
      this.dispatchFindIfReady()
    }

    if (changes['barcodes']) {
      this.renderAllBarcodeLayers()
    }
  }

  ngAfterViewInit(): void {
    this.setupResizeObserver()
    this.initViewer()
    if (!this.hasLoaded) {
      this.loadDocument()
      return
    }
    if (this.pdf) {
      this.applyViewerState()
    }
  }

  ngOnDestroy(): void {
    this.eventBus.off('pagerendered', this.onPageRendered)
    this.eventBus.off('pagesinit', this.onPagesInit)
    this.eventBus.off('pagechanging', this.onPageChanging)
    this.resizeObserver?.disconnect()
    this.copyTimeouts.forEach((timeout) => clearTimeout(timeout))
    this.loadingTask?.destroy()
    this.pdfViewer?.cleanup()
    this.pdfViewer = undefined
  }

  private resetViewerState(): void {
    this.hasLoaded = false
    this.hasRenderedPage = false
    this.lastFindQuery = ''
    this.lastViewerPage = undefined
    this.loadingTask?.destroy()
    this.loadingTask = undefined
    this.pdf = undefined
    this.linkService.setDocument(null)
    if (this.pdfViewer) {
      this.pdfViewer.setDocument(null)
      this.pdfViewer.currentPageNumber = 1
    }
  }

  private async loadDocument(): Promise<void> {
    if (this.hasLoaded) {
      return
    }

    this.hasLoaded = true
    this.hasRenderedPage = false
    this.lastFindQuery = ''
    this.loadingTask?.destroy()

    GlobalWorkerOptions.workerSrc = new URL(
      'assets/js/pdf.worker.min.mjs',
      this.document.baseURI
    ).toString()
    const initOptions = {
      url: this.src,
      password: this.password,
      withCredentials: true,
      wasmUrl: new URL('assets/wasm/', this.document.baseURI).toString(),
      iccUrl: new URL('assets/iccs/', this.document.baseURI).toString(),
    }
    this.loadingTask = getDocument(initOptions)
    try {
      const pdf = await this.loadingTask.promise
      this.pdf = pdf
      this.linkService.setDocument(pdf)
      this.findController.onIsPageVisible = () => true
      this.pdfViewer?.setDocument(pdf)
      this.applyViewerState()
      this.afterLoadComplete.emit(pdf)
    } catch (err) {
      this.loadError.emit(err)
    }
  }

  private setupResizeObserver(): void {
    this.resizeObserver?.disconnect()
    this.resizeObserver = new ResizeObserver(() => {
      this.applyScale()
    })
    this.resizeObserver.observe(this.container.nativeElement)
  }

  private initViewer(): void {
    this.viewer.nativeElement.innerHTML = ''
    this.pdfViewer?.cleanup()
    this.hasRenderedPage = false
    this.lastFindQuery = ''

    const textLayerMode = this.selectable === false ? 0 : 1
    const options = {
      container: this.container.nativeElement,
      viewer: this.viewer.nativeElement,
      eventBus: this.eventBus,
      linkService: this.linkService,
      findController: this.findController,
      textLayerMode,
      annotationMode: AnnotationMode.ENABLE,
      enableSelectionRendering: false,
      removePageBorders: true,
    }

    this.pdfViewer =
      this.renderMode === PdfRenderMode.Single
        ? new PDFSinglePageViewer(options)
        : new PDFViewer(options)
    this.linkService.setViewer(this.pdfViewer)

    this.eventBus.off('pagerendered', this.onPageRendered)
    this.eventBus.off('pagesinit', this.onPagesInit)
    this.eventBus.off('pagechanging', this.onPageChanging)
    this.eventBus.on('pagerendered', this.onPageRendered)
    this.eventBus.on('pagesinit', this.onPagesInit)
    this.eventBus.on('pagechanging', this.onPageChanging)

    if (this.pdf) {
      this.pdfViewer.setDocument(this.pdf)
      this.applyViewerState()
    }
  }

  private applyViewerState(applyScale = true): void {
    if (!this.pdfViewer) {
      return
    }
    const hasPages = this.pdfViewer.pagesCount > 0
    if (typeof this.rotation === 'number' && hasPages) {
      this.pdfViewer.pagesRotation = this.rotation
    }
    if (
      typeof this.page === 'number' &&
      hasPages &&
      this.page !== this.lastViewerPage
    ) {
      const nextPage = Math.min(
        Math.max(Math.trunc(this.page), 1),
        this.pdfViewer.pagesCount
      )
      if (nextPage !== this.pdfViewer.currentPageNumber) {
        this.pdfViewer.currentPageNumber = nextPage
      }
    }
    if (this.page === this.lastViewerPage) {
      this.lastViewerPage = undefined
    }
    if (hasPages && applyScale) {
      this.applyScale()
    }
    this.dispatchFindIfReady()
  }

  private applyScale(): void {
    if (!this.pdfViewer) {
      return
    }
    if (this.pdfViewer.pagesCount === 0) {
      return
    }
    const zoomFactor = Number(this.zoom) || 1
    this.pdfViewer.currentScaleValue = this.zoomScale
    if (zoomFactor !== 1) {
      this.pdfViewer.currentScale = this.pdfViewer.currentScale * zoomFactor
    }
  }

  private dispatchFindIfReady(): void {
    if (!this.hasRenderedPage) {
      return
    }
    const query = this.searchQuery?.trim()
    if (query === this.lastFindQuery) {
      return
    }
    this.lastFindQuery = query
    this.eventBus.dispatch('find', {
      query,
      caseSensitive: false,
      highlightAll: query?.length > 0,
      phraseSearch: true,
    })
  }

  private renderAllBarcodeLayers(): void {
    const viewer = this.pdfViewer as unknown as {
      pagesCount: number
      getPageView?: (index: number) => BarcodePageView
    }
    if (!viewer?.getPageView) {
      return
    }
    for (let index = 0; index < viewer.pagesCount; index++) {
      this.renderBarcodeLayer(viewer.getPageView(index))
    }
  }

  private renderBarcodeLayer(pageView?: BarcodePageView): void {
    if (!pageView?.div || !pageView.viewport) {
      return
    }
    pageView.div.querySelector(':scope > .barcodeLayer')?.remove()
    const barcodes = (this.barcodes ?? []).filter(
      (barcode) => barcode.page === pageView.id && barcode.rect?.length === 4
    )
    if (!barcodes.length) {
      return
    }

    const document = pageView.div.ownerDocument
    const { viewport } = pageView
    const layer = document.createElement('div')
    layer.className = 'barcodeLayer'

    for (const barcode of barcodes) {
      const [x0, y0, x1, y1] = barcode.rect
      const [ax, ay] = viewport.convertToViewportPoint(x0, y0)
      const [bx, by] = viewport.convertToViewportPoint(x1, y1)
      // Relative to the page, so the layer follows zoom changes until the
      // page is rendered again
      const region = document.createElement('div')
      region.className = 'barcode-region'
      region.title = barcode.value
      region.style.left = `${(Math.min(ax, bx) / viewport.width) * 100}%`
      region.style.top = `${(Math.min(ay, by) / viewport.height) * 100}%`
      region.style.width = `${(Math.abs(bx - ax) / viewport.width) * 100}%`
      region.style.height = `${(Math.abs(by - ay) / viewport.height) * 100}%`

      if (isLinkValue(barcode.value)) {
        const link = document.createElement('a')
        link.className = 'barcode-link'
        link.href = barcode.value.trim()
        link.target = '_blank'
        link.rel = 'noopener noreferrer nofollow'
        region.appendChild(link)
      }

      const button = document.createElement('button')
      button.type = 'button'
      button.className = 'barcode-copy btn btn-sm'
      button.textContent = $localize`Copy`
      button.setAttribute('aria-label', $localize`Copy barcode content`)
      button.addEventListener('click', (event) => {
        event.preventDefault()
        event.stopPropagation()
        if (!this.clipboard.copy(barcode.value)) {
          return
        }
        button.textContent = $localize`Copied!`
        const timeout = setTimeout(() => {
          button.textContent = $localize`Copy`
          this.copyTimeouts.delete(timeout)
        }, 2000)
        this.copyTimeouts.add(timeout)
      })
      region.appendChild(button)
      layer.appendChild(region)
    }

    pageView.div.appendChild(layer)
  }
}

interface BarcodePageView {
  id: number
  div?: HTMLElement
  viewport?: {
    width: number
    height: number
    convertToViewportPoint: (x: number, y: number) => number[]
  }
}

function isLinkValue(value: string): boolean {
  try {
    const url = new URL(value.trim())
    return ['http:', 'https:'].includes(url.protocol) && !!url.host
  } catch {
    return false
  }
}
