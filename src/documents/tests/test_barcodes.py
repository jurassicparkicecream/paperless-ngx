import shutil
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import pytest
from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase
from django.test import override_settings
from pikepdf import Name
from pikepdf import Pdf
from rest_framework import status
from rest_framework.test import APIClient

from documents import tasks
from documents.barcodes import BarcodePlugin
from documents.barcodes import LocatedBarcode
from documents.barcodes import add_barcode_links
from documents.barcodes import attach_barcode_rects
from documents.barcodes import is_link_url
from documents.barcodes import locate_barcodes
from documents.consumer import ConsumerError
from documents.data_models import ConsumableDocument
from documents.data_models import DocumentMetadataOverrides
from documents.data_models import DocumentSource
from documents.models import Document
from documents.models import Tag
from documents.plugins.base import StopConsumeTaskError
from documents.tests.utils import ConsumeTaskMixin
from documents.tests.utils import SampleDirMixin
from paperless.config import BarcodeConfig
from paperless.models import ApplicationConfiguration
from paperless_testing.assertions import FileSystemAssertsMixin
from paperless_testing.dirs import DirectoriesMixin
from paperless_testing.fakes.progress import FakeProgressManager


class GetReaderPluginMixin:
    @contextmanager
    def get_reader(self, filepath: Path) -> Generator[BarcodePlugin, None, None]:
        reader = BarcodePlugin(
            ConsumableDocument(DocumentSource.ConsumeFolder, original_file=filepath),
            DocumentMetadataOverrides(),
            FakeProgressManager(filepath.name, None),
            self.dirs.scratch_dir,
            "task-id",
        )
        reader.setup()
        yield reader
        reader.cleanup()


class TestBarcode(
    DirectoriesMixin,
    FileSystemAssertsMixin,
    SampleDirMixin,
    GetReaderPluginMixin,
    TestCase,
):
    def test_scan_file_for_separating_barcodes(self) -> None:
        """
        GIVEN:
            - PDF containing barcodes
        WHEN:
            - File is scanned for barcodes
        THEN:
            - Correct page index located
        """
        test_file = self.BARCODE_SAMPLE_DIR / "patch-code-t.pdf"

        with self.get_reader(test_file) as reader:
            reader.detect()
            separator_page_numbers = reader.get_separation_pages()

            self.assertEqual(reader.pdf_file, test_file)
            self.assertDictEqual(separator_page_numbers, {0: False})

    @override_settings(
        CONSUMER_BARCODE_TIFF_SUPPORT=True,
    )
    def test_scan_tiff_for_separating_barcodes(self) -> None:
        """
        GIVEN:
            - TIFF image containing barcodes
        WHEN:
            - Consume task returns
        THEN:
            - The file was split
        """
        test_file = self.BARCODE_SAMPLE_DIR / "patch-code-t-middle.tiff"

        with self.get_reader(test_file) as reader:
            reader.detect()
            separator_page_numbers = reader.get_separation_pages()

            self.assertDictEqual(separator_page_numbers, {1: False})

    @override_settings(CONSUMER_ENABLE_ASN_BARCODE=True)
    @pytest.mark.usefixtures("fake_progress_manager")
    def test_asn_barcode_duplicate_in_trash_fails(self) -> None:
        """
        GIVEN:
            - A document with ASN barcode 123 is in the trash
        WHEN:
            - A file with the same barcode ASN is consumed
        THEN:
            - The ASN check is re-run and consumption fails
        """
        test_file = self.BARCODE_SAMPLE_DIR / "barcode-39-asn-123.pdf"

        first_doc = Document.objects.create(
            title="First ASN 123",
            content="",
            checksum="asn123first",
            mime_type="application/pdf",
            archive_serial_number=123,
        )

        first_doc.delete()

        dupe_asn = settings.SCRATCH_DIR / "barcode-39-asn-123-second.pdf"
        shutil.copy(test_file, dupe_asn)

        with self.assertRaisesRegex(ConsumerError, r"ASN 123.*trash"):
            tasks.consume_file(
                ConsumableDocument(
                    source=DocumentSource.ConsumeFolder,
                    original_file=dupe_asn,
                ),
                None,
            )

    @override_settings(
        CONSUMER_BARCODE_TIFF_SUPPORT=True,
    )
    def test_scan_tiff_with_alpha_for_separating_barcodes(self) -> None:
        """
        GIVEN:
            - TIFF image containing barcodes
        WHEN:
            - Consume task returns
        THEN:
            - The file was split
        """
        test_file = self.BARCODE_SAMPLE_DIR / "patch-code-t-middle-alpha.tiff"

        with self.get_reader(test_file) as reader:
            reader.detect()
            separator_page_numbers = reader.get_separation_pages()

            self.assertDictEqual(separator_page_numbers, {1: False})

    def test_scan_file_for_separating_barcodes_none_present(self) -> None:
        """
        GIVEN:
            - File with no barcodes
        WHEN:
            - File is scanned
        THEN:
            - No barcodes detected
            - No pages to split on
        """
        test_file = self.SAMPLE_DIR / "simple.pdf"
        with self.get_reader(test_file) as reader:
            reader.detect()
            separator_page_numbers = reader.get_separation_pages()

            self.assertEqual(reader.pdf_file, test_file)
            self.assertDictEqual(separator_page_numbers, {})

    def test_scan_file_for_separating_barcodes_middle_page(self) -> None:
        """
        GIVEN:
            - PDF file containing a separator on page 1 (zero indexed)
        WHEN:
            - File is scanned for barcodes
        THEN:
            - Barcode is detected on page 1 (zero indexed)
        """
        test_file = self.BARCODE_SAMPLE_DIR / "patch-code-t-middle.pdf"

        with self.get_reader(test_file) as reader:
            reader.detect()
            separator_page_numbers = reader.get_separation_pages()

            self.assertEqual(reader.pdf_file, test_file)
            self.assertDictEqual(separator_page_numbers, {1: False})

    def test_scan_file_for_separating_barcodes_multiple_pages(self) -> None:
        """
        GIVEN:
            - PDF file containing a separator on pages 2 and 5 (zero indexed)
        WHEN:
            - File is scanned for barcodes
        THEN:
            - Barcode is detected on pages 2 and 5 (zero indexed)
        """
        test_file = self.BARCODE_SAMPLE_DIR / "several-patcht-codes.pdf"

        with self.get_reader(test_file) as reader:
            reader.detect()
            separator_page_numbers = reader.get_separation_pages()

            self.assertEqual(reader.pdf_file, test_file)
            self.assertDictEqual(separator_page_numbers, {2: False, 5: False})

    def test_scan_file_for_separating_barcodes_hard_to_detect(self) -> None:
        """
        GIVEN:
            - PDF file containing a separator on page 1 (zero indexed)
            - The barcode is upside down, fuzzy or distorted
        WHEN:
            - File is scanned for barcodes
        THEN:
            - Barcode is detected on page 1 (zero indexed)
        """

        for test_file in [
            "patch-code-t-middle-reverse.pdf",
            "patch-code-t-middle-distorted.pdf",
            "patch-code-t-middle-fuzzy.pdf",
        ]:
            test_file = self.BARCODE_SAMPLE_DIR / test_file

            with self.get_reader(test_file) as reader:
                reader.detect()
                separator_page_numbers = reader.get_separation_pages()

                self.assertEqual(reader.pdf_file, test_file)
                self.assertDictEqual(separator_page_numbers, {1: False})

    def test_scan_file_for_separating_barcodes_unreadable(self) -> None:
        """
        GIVEN:
            - PDF file containing a separator on page 1 (zero indexed)
            - The barcode is not readable
        WHEN:
            - File is scanned for barcodes
        THEN:
            - Barcode is detected on page 1 (zero indexed)
        """
        test_file = self.BARCODE_SAMPLE_DIR / "patch-code-t-middle-unreadable.pdf"

        with self.get_reader(test_file) as reader:
            reader.detect()
            separator_page_numbers = reader.get_separation_pages()

            self.assertEqual(reader.pdf_file, test_file)
            self.assertDictEqual(separator_page_numbers, {})

    def test_scan_file_for_separating_barcodes_fax_decode(self) -> None:
        """
        GIVEN:
            - A PDF containing an image encoded as CCITT Group 4 encoding
        WHEN:
            - Barcode processing happens with the file
        THEN:
            - The barcode is still detected
        """
        test_file = self.BARCODE_SAMPLE_DIR / "barcode-fax-image.pdf"

        with self.get_reader(test_file) as reader:
            reader.detect()
            separator_page_numbers = reader.get_separation_pages()

            self.assertEqual(reader.pdf_file, test_file)
            self.assertDictEqual(separator_page_numbers, {1: False})

    def test_scan_file_for_separating_qr_barcodes(self) -> None:
        """
        GIVEN:
            - PDF file containing a separator on page 0 (zero indexed)
            - The barcode is a QR code
        WHEN:
            - File is scanned for barcodes
        THEN:
            - Barcode is detected on page 0 (zero indexed)
        """
        test_file = self.BARCODE_SAMPLE_DIR / "patch-code-t-qr.pdf"

        with self.get_reader(test_file) as reader:
            reader.detect()
            separator_page_numbers = reader.get_separation_pages()

            self.assertEqual(reader.pdf_file, test_file)
            self.assertDictEqual(separator_page_numbers, {0: False})

    @override_settings(CONSUMER_BARCODE_STRING="CUSTOM BARCODE")
    def test_scan_file_for_separating_custom_barcodes(self) -> None:
        """
        GIVEN:
            - PDF file containing a separator on page 0 (zero indexed)
            - The barcode separation value is customized
        WHEN:
            - File is scanned for barcodes
        THEN:
            - Barcode is detected on page 0 (zero indexed)
        """
        test_file = self.BARCODE_SAMPLE_DIR / "barcode-39-custom.pdf"

        with self.get_reader(test_file) as reader:
            reader.detect()
            separator_page_numbers = reader.get_separation_pages()

            self.assertEqual(reader.pdf_file, test_file)
            self.assertDictEqual(separator_page_numbers, {0: False})

    @override_settings(CONSUMER_BARCODE_STRING="CUSTOM BARCODE")
    def test_scan_file_for_separating_custom_qr_barcodes(self) -> None:
        """
        GIVEN:
            - PDF file containing a separator on page 0 (zero indexed)
            - The barcode separation value is customized
            - The barcode is a QR code
        WHEN:
            - File is scanned for barcodes
        THEN:
            - Barcode is detected on page 0 (zero indexed)
        """
        test_file = self.BARCODE_SAMPLE_DIR / "barcode-qr-custom.pdf"

        with self.get_reader(test_file) as reader:
            reader.detect()
            separator_page_numbers = reader.get_separation_pages()

            self.assertEqual(reader.pdf_file, test_file)
            self.assertDictEqual(separator_page_numbers, {0: False})

    @override_settings(CONSUMER_BARCODE_STRING="CUSTOM BARCODE")
    def test_scan_file_for_separating_custom_128_barcodes(self) -> None:
        """
        GIVEN:
            - PDF file containing a separator on page 0 (zero indexed)
            - The barcode separation value is customized
            - The barcode is a 128 code
        WHEN:
            - File is scanned for barcodes
        THEN:
            - Barcode is detected on page 0 (zero indexed)
        """
        test_file = self.BARCODE_SAMPLE_DIR / "barcode-128-custom.pdf"

        with self.get_reader(test_file) as reader:
            reader.detect()
            separator_page_numbers = reader.get_separation_pages()

            self.assertEqual(reader.pdf_file, test_file)
            self.assertDictEqual(separator_page_numbers, {0: False})

    def test_scan_file_for_separating_wrong_qr_barcodes(self) -> None:
        """
        GIVEN:
            - PDF file containing a separator on page 0 (zero indexed)
            - The barcode value is customized
            - The separation value is NOT customized
        WHEN:
            - File is scanned for barcodes
        THEN:
            - No split pages are detected
        """
        test_file = self.BARCODE_SAMPLE_DIR / "barcode-39-custom.pdf"

        with self.get_reader(test_file) as reader:
            reader.detect()
            separator_page_numbers = reader.get_separation_pages()

            self.assertEqual(reader.pdf_file, test_file)
            self.assertDictEqual(separator_page_numbers, {})

    @override_settings(CONSUMER_BARCODE_STRING="ADAR-NEXTDOC")
    def test_scan_file_qr_barcodes_was_problem(self) -> None:
        """
        GIVEN:
            - Input PDF with certain QR codes that aren't detected at current size
        WHEN:
            - The input file is scanned for barcodes
        THEN:
            - QR codes are detected
        """
        test_file = self.BARCODE_SAMPLE_DIR / "many-qr-codes.pdf"

        with self.get_reader(test_file) as reader:
            reader.detect()
            separator_page_numbers = reader.get_separation_pages()

            self.assertEqual(reader.pdf_file, test_file)
            self.assertGreater(len(reader.barcodes), 0)
            self.assertDictEqual(separator_page_numbers, {1: False})

    def test_scan_file_for_separating_barcodes_password(self) -> None:
        """
        GIVEN:
            - Password protected PDF
        WHEN:
            - File is scanned for barcode
        THEN:
            - Scanning handles the exception without crashing
        """
        test_file = self.SAMPLE_DIR / "password-is-test.pdf"
        with self.assertLogs("paperless.barcodes", level="WARNING") as cm:
            with self.get_reader(test_file) as reader:
                reader.detect()
                warning = cm.output[0]
                expected_str = "WARNING:paperless.barcodes:File is likely password protected, not checking for barcodes"
                self.assertTrue(warning.startswith(expected_str))

                separator_page_numbers = reader.get_separation_pages()

                self.assertEqual(reader.pdf_file, test_file)
                self.assertDictEqual(separator_page_numbers, {})

    def test_separate_pages(self) -> None:
        """
        GIVEN:
            - Input PDF 2 pages after separation
        WHEN:
            - The input file separated at the barcode
        THEN:
            - Two new documents are produced
        """
        test_file = self.BARCODE_SAMPLE_DIR / "patch-code-t-middle.pdf"

        with self.get_reader(test_file) as reader:
            documents = reader.separate_pages({1: False})

            self.assertEqual(reader.pdf_file, test_file)
            self.assertEqual(len(documents), 2)

    def test_separate_pages_double_code(self) -> None:
        """
        GIVEN:
            - Input PDF with two patch code pages in a row
        WHEN:
            - The input file is split
        THEN:
            - Only two files are output
        """
        test_file = self.BARCODE_SAMPLE_DIR / "patch-code-t-double.pdf"

        with self.get_reader(test_file) as reader:
            documents = reader.separate_pages({1: False, 2: False})

            self.assertEqual(len(documents), 2)

    @override_settings(CONSUMER_ENABLE_BARCODES=True)
    def test_separate_pages_no_list(self) -> None:
        """
        GIVEN:
            - Input file to separate
        WHEN:
            - No separation pages are provided
        THEN:
            - Nothing happens
        """
        test_file = self.SAMPLE_DIR / "simple.pdf"

        with self.get_reader(test_file) as reader:
            try:
                reader.run()
            except StopConsumeTaskError:
                self.fail("Barcode reader split pages unexpectedly")

    @override_settings(
        CONSUMER_ENABLE_BARCODES=True,
        CONSUMER_BARCODE_TIFF_SUPPORT=True,
    )
    def test_consume_barcode_unsupported_jpg_file(self) -> None:
        """
        GIVEN:
            - JPEG image as input
        WHEN:
            - Consume task returns
        THEN:
            - Barcode reader reported warning
            - Consumption continued with the file
        """
        test_file = self.SAMPLE_DIR / "simple.jpg"

        with self.get_reader(test_file) as reader:
            self.assertFalse(reader.able_to_run)

    @override_settings(
        CONSUMER_ENABLE_BARCODES=True,
        CONSUMER_ENABLE_ASN_BARCODE=True,
    )
    def test_separate_pages_by_asn_barcodes_and_patcht(self) -> None:
        """
        GIVEN:
            - Input PDF with a patch code on page 3 and ASN barcodes on pages 1,5,6,9,11
        WHEN:
            - Input file is split on barcodes
        THEN:
            - Correct number of files produced, split correctly by correct pages
        """
        test_file = self.BARCODE_SAMPLE_DIR / "split-by-asn-2.pdf"

        with self.get_reader(test_file) as reader:
            reader.detect()
            separator_page_numbers = reader.get_separation_pages()

            self.assertEqual(
                reader.pdf_file,
                test_file,
            )
            self.assertDictEqual(
                separator_page_numbers,
                {
                    2: False,
                    4: True,
                    5: True,
                    8: True,
                    10: True,
                },
            )

            document_list = reader.separate_pages(separator_page_numbers)
            self.assertEqual(len(document_list), 6)

    @override_settings(
        CONSUMER_ENABLE_BARCODES=True,
        CONSUMER_ENABLE_ASN_BARCODE=True,
    )
    def test_separate_pages_by_asn_barcodes(self) -> None:
        """
        GIVEN:
            - Input PDF with ASN barcodes on pages 1,3,4,7,9
        WHEN:
            - Input file is split on barcodes
        THEN:
            - Correct number of files produced, split correctly by correct pages
        """
        test_file = self.BARCODE_SAMPLE_DIR / "split-by-asn-1.pdf"

        with self.get_reader(test_file) as reader:
            reader.detect()
            separator_page_numbers = reader.get_separation_pages()

            self.assertEqual(reader.pdf_file, test_file)
            self.assertDictEqual(
                separator_page_numbers,
                {
                    2: True,
                    3: True,
                    6: True,
                    8: True,
                },
            )

            document_list = reader.separate_pages(separator_page_numbers)
            self.assertEqual(len(document_list), 5)

    @override_settings(
        CONSUMER_ENABLE_BARCODES=True,
        CONSUMER_ENABLE_ASN_BARCODE=True,
        CONSUMER_BARCODE_RETAIN_SPLIT_PAGES=True,
    )
    def test_separate_pages_by_asn_barcodes_and_patcht_retain_pages(self) -> None:
        """
        GIVEN:
            - Input PDF with a patch code on page 3 and ASN barcodes on pages 1,5,6,9,11
            - Retain split pages is enabled
        WHEN:
            - Input file is split on barcodes
        THEN:
            - Correct number of files produced, split correctly by correct pages, and the split pages are retained
        """
        test_file = self.BARCODE_SAMPLE_DIR / "split-by-asn-2.pdf"

        with self.get_reader(test_file) as reader:
            reader.detect()
            separator_page_numbers = reader.get_separation_pages()

            self.assertEqual(
                reader.pdf_file,
                test_file,
            )
            self.assertDictEqual(
                separator_page_numbers,
                {
                    2: True,
                    4: True,
                    5: True,
                    8: True,
                    10: True,
                },
            )

    def test_barcode_config(self) -> None:
        """
        GIVEN:
            - Barcode app config is set (settings are not)
        WHEN:
            - Document with barcode is processed
        THEN:
            - The barcode config is used
        """
        app_config = ApplicationConfiguration.objects.first()
        assert app_config is not None
        app_config.barcodes_enabled = True
        app_config.barcode_string = "CUSTOM BARCODE"
        app_config.save()
        test_file = self.BARCODE_SAMPLE_DIR / "barcode-39-custom.pdf"
        with self.get_reader(test_file) as reader:
            reader.detect()
            separator_page_numbers = reader.get_separation_pages()

            self.assertEqual(reader.pdf_file, test_file)
            self.assertDictEqual(separator_page_numbers, {0: False})


class TestBarcodeNewConsume(
    DirectoriesMixin,
    FileSystemAssertsMixin,
    SampleDirMixin,
    ConsumeTaskMixin,
    TestCase,
):
    @override_settings(CONSUMER_ENABLE_BARCODES=True)
    @pytest.mark.usefixtures("fake_progress_manager")
    def test_consume_barcode_file(self) -> None:
        """
        GIVEN:
            - Incoming file with at 1 barcode producing 2 documents
            - Document includes metadata override information
        WHEN:
            - The document is split
        THEN:
            - Two new consume tasks are created
            - Metadata overrides are preserved for the new consume
            - The document source is unchanged (for consume templates)
        """
        test_file = self.BARCODE_SAMPLE_DIR / "patch-code-t-middle.pdf"
        temp_copy = self.dirs.scratch_dir / test_file.name
        shutil.copy(test_file, temp_copy)

        overrides = DocumentMetadataOverrides(tag_ids=[1, 2, 9])

        self.assertEqual(
            tasks.consume_file(
                ConsumableDocument(
                    source=DocumentSource.ConsumeFolder,
                    original_file=temp_copy,
                ),
                overrides,
            ),
            {"reason": "Barcode splitting complete!"},
        )
        # 2 new document consume tasks created
        self.assertEqual(self.consume_file_mock.call_count, 2)

        self.assertIsNotFile(temp_copy)

        # Check the split files exist
        # Check the original_path is set
        # Check the source is unchanged
        # Check the overrides are unchanged
        for (
            new_input_doc,
            new_doc_overrides,
        ) in self.get_all_consume_task_call_args():
            self.assertIsFile(new_input_doc.original_file)
            self.assertEqual(new_input_doc.original_path, temp_copy)
            self.assertEqual(new_input_doc.source, DocumentSource.ConsumeFolder)
            self.assertEqual(overrides, new_doc_overrides)


class TestAsnBarcode(DirectoriesMixin, SampleDirMixin, GetReaderPluginMixin, TestCase):
    @contextmanager
    def get_reader(self, filepath: Path) -> BarcodePlugin:
        reader = BarcodePlugin(
            ConsumableDocument(DocumentSource.ConsumeFolder, original_file=filepath),
            DocumentMetadataOverrides(),
            FakeProgressManager(filepath.name, None),
            self.dirs.scratch_dir,
            "task-id",
        )
        reader.setup()
        yield reader
        reader.cleanup()

    @override_settings(CONSUMER_ASN_BARCODE_PREFIX="CUSTOM-PREFIX-")
    def test_scan_file_for_asn_custom_prefix(self) -> None:
        """
        GIVEN:
            - PDF containing an ASN barcode with custom prefix
            - The ASN value is 123
        WHEN:
            - File is scanned for barcodes
        THEN:
            - The ASN is located
            - The ASN integer value is correct
        """
        test_file = self.BARCODE_SAMPLE_DIR / "barcode-39-asn-custom-prefix.pdf"
        with self.get_reader(test_file) as reader:
            asn = reader.asn

            self.assertEqual(reader.pdf_file, test_file)
            self.assertEqual(asn, 123)

    def test_scan_file_for_asn_barcode(self) -> None:
        """
        GIVEN:
            - PDF containing an ASN barcode
            - The ASN value is 123
        WHEN:
            - File is scanned for barcodes
        THEN:
            - The ASN is located
            - The ASN integer value is correct
        """
        test_file = self.BARCODE_SAMPLE_DIR / "barcode-39-asn-123.pdf"

        with self.get_reader(test_file) as reader:
            asn = reader.asn

            self.assertEqual(reader.pdf_file, test_file)
            self.assertEqual(asn, 123)

    def test_scan_file_for_asn_not_found(self) -> None:
        """
        GIVEN:
            - PDF without an ASN barcode
        WHEN:
            - File is scanned for barcodes
        THEN:
            - No ASN is retrieved from the document
        """
        test_file = self.BARCODE_SAMPLE_DIR / "patch-code-t.pdf"

        with self.get_reader(test_file) as reader:
            asn = reader.asn

            self.assertEqual(reader.pdf_file, test_file)
            self.assertEqual(asn, None)

    def test_scan_file_for_asn_barcode_invalid(self) -> None:
        """
        GIVEN:
            - PDF containing an ASN barcode
            - The ASN value is XYZXYZ
        WHEN:
            - File is scanned for barcodes
        THEN:
            - The ASN is located
            - The ASN value is not used
        """
        test_file = self.BARCODE_SAMPLE_DIR / "barcode-39-asn-invalid.pdf"

        with self.get_reader(test_file) as reader:
            asn = reader.asn

            self.assertEqual(reader.pdf_file, test_file)

            self.assertEqual(reader.pdf_file, test_file)
            self.assertEqual(asn, None)

    @override_settings(CONSUMER_ENABLE_ASN_BARCODE=True)
    @pytest.mark.usefixtures("fake_progress_manager")
    def test_consume_barcode_file_asn_assignment(self) -> None:
        """
        GIVEN:
            - PDF containing an ASN barcode
            - The ASN value is 123
        WHEN:
            - File is scanned for barcodes
        THEN:
            - The ASN is located
            - The ASN integer value is correct
            - The ASN is provided as the override value to the consumer
        """
        test_file = self.BARCODE_SAMPLE_DIR / "barcode-39-asn-123.pdf"

        dst = settings.SCRATCH_DIR / "barcode-39-asn-123.pdf"
        shutil.copy(test_file, dst)

        tasks.consume_file(
            ConsumableDocument(
                source=DocumentSource.ConsumeFolder,
                original_file=dst,
            ),
            None,
        )

        document = Document.objects.first()
        assert document is not None

        self.assertEqual(document.archive_serial_number, 123)

    def test_scan_file_for_qrcode_without_upscale(self) -> None:
        """
        GIVEN:
            - A printed and scanned PDF document with a rather small QR code
        WHEN:
            - ASN barcode detection is run with default settings
        THEN:
            - ASN 123 is detected
        """

        test_file = self.BARCODE_SAMPLE_DIR / "barcode-qr-asn-000123-upscale-dpi.pdf"

        with self.get_reader(test_file) as reader:
            reader.detect()
            self.assertEqual(len(reader.barcodes), 1)
            self.assertEqual(reader.asn, 123)

    @override_settings(CONSUMER_BARCODE_DPI=600)
    @override_settings(CONSUMER_BARCODE_UPSCALE=1.5)
    def test_scan_file_for_qrcode_with_upscale(self) -> None:
        """
        GIVEN:
            - A printed and scanned PDF document with a rather small QR code
        WHEN:
            - ASN barcode detection is run with 600dpi and an upscale factor of 1.5
        THEN:
            - ASN 123 is detected
        """

        test_file = self.BARCODE_SAMPLE_DIR / "barcode-qr-asn-000123-upscale-dpi.pdf"

        with self.get_reader(test_file) as reader:
            reader.detect()
            self.assertEqual(len(reader.barcodes), 1)
            self.assertEqual(reader.asn, 123)


class TestTagBarcode(DirectoriesMixin, SampleDirMixin, GetReaderPluginMixin, TestCase):
    @contextmanager
    def get_reader(self, filepath: Path) -> BarcodePlugin:
        reader = BarcodePlugin(
            ConsumableDocument(DocumentSource.ConsumeFolder, original_file=filepath),
            DocumentMetadataOverrides(),
            FakeProgressManager(filepath.name, None),
            self.dirs.scratch_dir,
            "task-id",
        )
        reader.setup()
        yield reader
        reader.cleanup()

    @override_settings(
        CONSUMER_ENABLE_TAG_BARCODE=True,
        CONSUMER_TAG_BARCODE_MAPPING={"TAG:(.*)": "\\g<1>"},
    )
    def test_barcode_without_tag_match(self) -> None:
        """
        GIVEN:
            - Barcode that does not match any TAG mapping pattern
            - TAG mapping configured for "TAG:" prefix only
        WHEN:
            - is_tag property is checked on an ASN barcode
        THEN:
            - Returns False
        """
        test_file = self.BARCODE_SAMPLE_DIR / "barcode-39-asn-123.pdf"
        with self.get_reader(test_file) as reader:
            reader.detect()

            self.assertGreater(
                len(reader.barcodes),
                0,
                "Should have detected at least one barcode",
            )
            asn_barcode = reader.barcodes[0]
            self.assertFalse(
                asn_barcode.is_tag,
                f"ASN barcode '{asn_barcode.value}' should not match TAG: pattern",
            )

    @override_settings(CONSUMER_ENABLE_TAG_BARCODE=True)
    def test_scan_file_without_matching_barcodes(self) -> None:
        """
        GIVEN:
            - PDF containing tag barcodes but none with matching prefix (default "TAG:")
        WHEN:
            - File is scanned for barcodes
        THEN:
            - No TAG has been created
        """
        test_file = self.BARCODE_SAMPLE_DIR / "barcode-39-asn-custom-prefix.pdf"
        with self.get_reader(test_file) as reader:
            reader.run()
            tags = reader.metadata.tag_ids
            self.assertEqual(tags, None)

    @override_settings(
        CONSUMER_ENABLE_TAG_BARCODE=False,
        CONSUMER_TAG_BARCODE_MAPPING={"CUSTOM-PREFIX-(.*)": "\\g<1>"},
    )
    def test_scan_file_with_matching_barcode_but_function_disabled(self) -> None:
        """
        GIVEN:
            - PDF containing a tag barcode with matching custom prefix
            - The tag barcode functionality is disabled
        WHEN:
            - File is scanned for barcodes
        THEN:
            - No TAG has been created
        """
        test_file = self.BARCODE_SAMPLE_DIR / "barcode-39-asn-custom-prefix.pdf"
        with self.get_reader(test_file) as reader:
            reader.run()
            tags = reader.metadata.tag_ids
            self.assertEqual(tags, None)

    @override_settings(
        CONSUMER_ENABLE_TAG_BARCODE=True,
        CONSUMER_TAG_BARCODE_MAPPING={"CUSTOM-PREFIX-(.*)": "\\g<1>"},
    )
    def test_scan_file_for_tag_custom_prefix(self) -> None:
        """
        GIVEN:
            - PDF containing a tag barcode with custom prefix
            - The barcode mapping accepts this prefix and removes it from the mapped tag value
            - The created tag is the non-prefixed values
        WHEN:
            - File is scanned for barcodes
        THEN:
            - The TAG is located
            - One TAG has been created
        """
        test_file = self.BARCODE_SAMPLE_DIR / "barcode-39-asn-custom-prefix.pdf"
        with self.get_reader(test_file) as reader:
            reader.metadata.tag_ids = [99]
            reader.run()
            self.assertEqual(reader.pdf_file, test_file)
            tags = reader.metadata.tag_ids
            self.assertEqual(len(tags), 2)
            self.assertEqual(tags[0], 99)
            self.assertEqual(Tag.objects.get(name__iexact="00123").pk, tags[1])

    @override_settings(
        CONSUMER_ENABLE_TAG_BARCODE=True,
        CONSUMER_TAG_BARCODE_MAPPING={"ASN(.*)": "\\g<1>"},
        CONSUMER_ENABLE_ASN_BARCODE=False,
    )
    def test_scan_file_for_many_custom_tags(self) -> None:
        """
        GIVEN:
            - PDF containing multiple tag barcode with custom prefix
            - The barcode mapping accepts this prefix and removes it from the mapped tag value
            - The created tags are the non-prefixed values
        WHEN:
            - File is scanned for barcodes
        THEN:
            - The TAG is located
            - File Tags have been created
        """
        test_file = self.BARCODE_SAMPLE_DIR / "split-by-asn-1.pdf"
        with self.get_reader(test_file) as reader:
            reader.run()
            tags = reader.metadata.tag_ids
            self.assertEqual(len(tags), 5)
            self.assertEqual(Tag.objects.get(name__iexact="00123").pk, tags[0])
            self.assertEqual(Tag.objects.get(name__iexact="00124").pk, tags[1])
            self.assertEqual(Tag.objects.get(name__iexact="00125").pk, tags[2])
            self.assertEqual(Tag.objects.get(name__iexact="00126").pk, tags[3])
            self.assertEqual(Tag.objects.get(name__iexact="00127").pk, tags[4])

    @override_settings(
        CONSUMER_ENABLE_TAG_BARCODE=True,
        CONSUMER_TAG_BARCODE_MAPPING={"CUSTOM-PREFIX-(.*)": "\\g<3>"},
    )
    def test_scan_file_for_tag_raises_value_error(self) -> None:
        """
        GIVEN:
            - Any error occurs during tag barcode processing
        THEN:
            - The processing should be skipped and not break the import
        """
        test_file = self.BARCODE_SAMPLE_DIR / "barcode-39-asn-custom-prefix.pdf"
        with self.get_reader(test_file) as reader:
            reader.run()
            # expect error to be caught and logged only
            tags = reader.metadata.tag_ids
            self.assertEqual(tags, None)

    @override_settings(
        CONSUMER_ENABLE_TAG_BARCODE=True,
        CONSUMER_TAG_BARCODE_SPLIT=True,
        CONSUMER_TAG_BARCODE_MAPPING={"TAG:(.*)": "\\g<1>"},
    )
    def test_split_on_tag_barcodes(self) -> None:
        """
        GIVEN:
            - PDF containing barcodes with TAG: prefix
            - Tag barcode splitting is enabled with TAG: mapping
        WHEN:
            - File is processed
        THEN:
            - Splits should occur at pages with TAG barcodes
            - Tags should NOT be assigned when tag splitting is enabled (they're assigned during re-consumption)
        """
        test_file = self.BARCODE_SAMPLE_DIR / "split-by-tag-basic.pdf"
        with self.get_reader(test_file) as reader:
            reader.detect()
            separator_page_numbers = reader.get_separation_pages()

            self.assertDictEqual(separator_page_numbers, {1: True, 3: True})

            tags = reader.metadata.tag_ids
            self.assertIsNone(tags)

    @override_settings(
        CONSUMER_ENABLE_TAG_BARCODE=True,
        CONSUMER_TAG_BARCODE_SPLIT=False,
        CONSUMER_TAG_BARCODE_MAPPING={"TAG:(.*)": "\\g<1>"},
    )
    def test_no_split_when_tag_split_disabled(self) -> None:
        """
        GIVEN:
            - PDF containing TAG barcodes (TAG:invoice, TAG:receipt)
            - Tag barcode splitting is disabled
        WHEN:
            - File is processed
        THEN:
            - No separation pages are identified
            - Tags are still extracted and assigned
        """
        test_file = self.BARCODE_SAMPLE_DIR / "split-by-tag-basic.pdf"
        with self.get_reader(test_file) as reader:
            reader.run()
            separator_page_numbers = reader.get_separation_pages()

            self.assertDictEqual(separator_page_numbers, {})

            tags = reader.metadata.tag_ids
            self.assertEqual(len(tags), 2)

    @override_settings(
        CONSUMER_ENABLE_BARCODES=True,
        CONSUMER_ENABLE_TAG_BARCODE=True,
        CONSUMER_TAG_BARCODE_SPLIT=True,
        CONSUMER_TAG_BARCODE_MAPPING={"TAG:(.*)": "\\g<1>"},
        CELERY_TASK_ALWAYS_EAGER=True,
        OCR_MODE="auto",
    )
    @pytest.mark.usefixtures("fake_progress_manager")
    def test_consume_barcode_file_tag_split_and_assignment(self) -> None:
        """
        GIVEN:
            - PDF containing TAG barcodes on pages 2 and 4 (TAG:invoice, TAG:receipt)
            - Tag barcode splitting is enabled
        WHEN:
            - File is consumed
        THEN:
            - PDF is split into 3 documents at barcode pages
            - Each split document has the appropriate TAG barcodes extracted and assigned
            - Document 1: page 1 (no tags)
            - Document 2: pages 2-3 with TAG:invoice
            - Document 3: pages 4-5 with TAG:receipt
        """
        test_file = self.BARCODE_SAMPLE_DIR / "split-by-tag-basic.pdf"
        dst = settings.SCRATCH_DIR / "split-by-tag-basic.pdf"
        shutil.copy(test_file, dst)

        result = tasks.consume_file(
            ConsumableDocument(
                source=DocumentSource.ConsumeFolder,
                original_file=dst,
            ),
            None,
        )

        self.assertEqual(result, {"reason": "Barcode splitting complete!"})

        documents = Document.objects.all().order_by("id")
        self.assertEqual(documents.count(), 3)

        doc1 = documents[0]
        self.assertEqual(doc1.tags.count(), 0)

        doc2 = documents[1]
        self.assertEqual(doc2.tags.count(), 1)
        _tag_1 = doc2.tags.first()
        assert _tag_1 is not None
        self.assertEqual(_tag_1.name, "invoice")

        doc3 = documents[2]
        self.assertEqual(doc3.tags.count(), 1)
        _tag_2 = doc3.tags.first()
        assert _tag_2 is not None
        self.assertEqual(_tag_2.name, "receipt")

    @override_settings(
        CONSUMER_ENABLE_TAG_BARCODE=True,
        CONSUMER_TAG_BARCODE_SPLIT=True,
        CONSUMER_TAG_BARCODE_MAPPING={"ASN(.*)": "ASN_\\g<1>", "TAG:(.*)": "\\g<1>"},
    )
    def test_split_by_mixed_asn_tag_backwards_compat(self) -> None:
        """
        GIVEN:
            - PDF with mixed ASN and TAG barcodes
            - Mapping that treats ASN barcodes as tags (backwards compatibility)
            - ASN12345 on page 1, TAG:personal on page 3, ASN13456 on page 5, TAG:business on page 7
        WHEN:
            - File is consumed
        THEN:
            - Both ASN and TAG barcodes trigger splits
            - Split points are at pages 3, 5, and 7 (page 1 never splits)
            - 4 separate documents are produced
        """
        test_file = self.BARCODE_SAMPLE_DIR / "split-by-tag-mixed-asn.pdf"

        with self.get_reader(test_file) as reader:
            reader.detect()
            separator_pages = reader.get_separation_pages()

            self.assertDictEqual(separator_pages, {2: True, 4: True, 6: True})

            document_list = reader.separate_pages(separator_pages)
            self.assertEqual(len(document_list), 4)

    @override_settings(
        CONSUMER_ENABLE_TAG_BARCODE=True,
        CONSUMER_TAG_BARCODE_SPLIT=True,
        CONSUMER_TAG_BARCODE_MAPPING={"TAG:(.*)": "\\g<1>"},
    )
    def test_split_by_tag_multiple_per_page(self) -> None:
        """
        GIVEN:
            - PDF with multiple TAG barcodes on same page
            - TAG:invoice and TAG:expense on page 2, TAG:receipt on page 4
        WHEN:
            - File is processed
        THEN:
            - Pages with barcodes trigger splits
            - Split points at pages 2 and 4
            - 3 separate documents are produced
        """
        test_file = self.BARCODE_SAMPLE_DIR / "split-by-tag-multiple-per-page.pdf"

        with self.get_reader(test_file) as reader:
            reader.detect()
            separator_pages = reader.get_separation_pages()

            self.assertDictEqual(separator_pages, {1: True, 3: True})

            document_list = reader.separate_pages(separator_pages)
            self.assertEqual(len(document_list), 3)


class TestBarcodeLinks(
    DirectoriesMixin,
    SampleDirMixin,
    GetReaderPluginMixin,
    TestCase,
):
    # Area of the pasted QR image on page 2 of barcode-qr-url.pdf, in PDF points
    QR_AREA = (403.2, 73.9, 547.2, 217.9)

    def _link_annotations(self, pdf_path: Path, page: int) -> list:
        with Pdf.open(pdf_path) as pdf:
            annots = pdf.pages[page].obj.get("/Annots", [])
            return [
                (str(a.A.URI), [float(x) for x in a.Rect], int(a.F))
                for a in annots
                if a.Subtype == Name.Link
            ]

    def _assert_over_qr(self, rect: list[float]) -> None:
        x0, y0, x1, y1 = self.QR_AREA
        self.assertGreaterEqual(rect[0], x0)
        self.assertGreaterEqual(rect[1], y0)
        self.assertLessEqual(rect[2], x1)
        self.assertLessEqual(rect[3], y1)
        # covers most of the code, not just a corner
        self.assertGreater(rect[2] - rect[0], 100)
        self.assertGreater(rect[3] - rect[1], 100)

    def test_is_link_url(self) -> None:
        """
        GIVEN:
            - Barcode values with various schemes
        WHEN:
            - Checked for being a link
        THEN:
            - Only absolute http(s) URLs are links
        """
        for value in (
            "https://example.com/a?b=c",
            "http://example.com",
            " https://example.com ",
        ):
            self.assertTrue(is_link_url(value), value)
        for value in (
            "javascript:alert(1)",
            "file:///etc/passwd",
            "mailto:someone@example.com",
            "https://",
            "example.com",
            "ASN00123",
            "",
        ):
            self.assertFalse(is_link_url(value), value)

    @override_settings(CONSUMER_ENABLE_BARCODE_LINKS=True)
    def test_link_pages_detected(self) -> None:
        """
        GIVEN:
            - PDF with a javascript: QR code on page 1 and a URL QR code on page 2
            - Barcode links enabled
        WHEN:
            - The barcode plugin runs
        THEN:
            - Only page 2 is remembered for linking
        """
        test_file = self.BARCODE_SAMPLE_DIR / "barcode-qr-url.pdf"

        with self.get_reader(test_file) as reader:
            self.assertTrue(reader.able_to_run)
            reader.run()
            self.assertEqual(reader.metadata.barcode_link_pages, [1])

    def test_link_pages_disabled(self) -> None:
        """
        GIVEN:
            - PDF with a URL QR code
            - Barcode links not enabled
        WHEN:
            - The barcode plugin runs
        THEN:
            - No pages are remembered for linking
        """
        test_file = self.BARCODE_SAMPLE_DIR / "barcode-qr-url.pdf"

        with self.get_reader(test_file) as reader:
            self.assertFalse(reader.able_to_run)
            reader.run()
            self.assertIsNone(reader.metadata.barcode_link_pages)

    def test_link_pages_from_app_config(self) -> None:
        """
        GIVEN:
            - Barcode links enabled in the application configuration only
        WHEN:
            - The barcode plugin checks whether it can run
        THEN:
            - It runs
        """
        app_config = ApplicationConfiguration.objects.first()
        app_config.barcode_enable_links = True
        app_config.save()

        test_file = self.BARCODE_SAMPLE_DIR / "barcode-qr-url.pdf"
        with self.get_reader(test_file) as reader:
            self.assertTrue(reader.able_to_run)

    def test_add_links(self) -> None:
        """
        GIVEN:
            - PDF with a javascript: QR code on page 1 and a URL QR code on page 2
        WHEN:
            - Links are added to all pages
        THEN:
            - One printable link to the URL covers the QR code on page 2
            - The javascript: code is not linked
        """
        test_file = self.dirs.scratch_dir / "barcode-qr-url.pdf"
        shutil.copy(self.BARCODE_SAMPLE_DIR / "barcode-qr-url.pdf", test_file)

        count = add_barcode_links(
            test_file,
            None,
            BarcodeConfig(),
            self.dirs.scratch_dir,
        )

        self.assertEqual(count, 1)
        self.assertEqual(self._link_annotations(test_file, 0), [])
        links = self._link_annotations(test_file, 1)
        self.assertEqual(len(links), 1)
        uri, rect, flags = links[0]
        self.assertEqual(uri, "https://example.com/invoice/4711")
        self.assertEqual(flags, 4)
        self._assert_over_qr(rect)

    def test_add_links_rotated_pages(self) -> None:
        """
        GIVEN:
            - The same page, displayed with each possible /Rotate value
        WHEN:
            - Links are added
        THEN:
            - The link always covers the QR code, as it is the same content
        """
        for rotate in (90, 180, 270):
            with self.subTest(rotate=rotate):
                test_file = self.dirs.scratch_dir / f"rotated-{rotate}.pdf"
                with Pdf.open(self.BARCODE_SAMPLE_DIR / "barcode-qr-url.pdf") as pdf:
                    pdf.pages[1].obj.Rotate = rotate
                    pdf.save(test_file)

                count = add_barcode_links(
                    test_file,
                    [1],
                    BarcodeConfig(),
                    self.dirs.scratch_dir,
                )

                self.assertEqual(count, 1)
                self._assert_over_qr(self._link_annotations(test_file, 1)[0][1])

    def test_add_links_only_given_pages(self) -> None:
        """
        GIVEN:
            - PDF with a URL QR code on page 2
        WHEN:
            - Links are added for page 1 only, or for a page that does not exist
        THEN:
            - Nothing is linked and the file is unchanged
        """
        test_file = self.dirs.scratch_dir / "barcode-qr-url.pdf"
        shutil.copy(self.BARCODE_SAMPLE_DIR / "barcode-qr-url.pdf", test_file)
        before = test_file.read_bytes()

        self.assertEqual(
            add_barcode_links(
                test_file,
                [0, 5],
                BarcodeConfig(),
                self.dirs.scratch_dir,
            ),
            0,
        )
        self.assertEqual(test_file.read_bytes(), before)

    def test_add_links_no_url_barcodes(self) -> None:
        """
        GIVEN:
            - PDF with barcodes that are not URLs
        WHEN:
            - Links are added
        THEN:
            - Nothing is linked
        """
        test_file = self.dirs.scratch_dir / "patch-code-t-qr.pdf"
        shutil.copy(self.BARCODE_SAMPLE_DIR / "patch-code-t-qr.pdf", test_file)

        self.assertEqual(
            add_barcode_links(test_file, None, BarcodeConfig(), self.dirs.scratch_dir),
            0,
        )

    @override_settings(
        CONSUMER_ENABLE_BARCODE_LINKS=True,
        CELERY_TASK_ALWAYS_EAGER=True,
        OCR_MODE="auto",
    )
    @pytest.mark.usefixtures("fake_progress_manager")
    def test_consume_file_links_archive(self) -> None:
        """
        GIVEN:
            - PDF with a URL QR code on page 2
            - Barcode links enabled
        WHEN:
            - File is consumed, then reprocessed
        THEN:
            - The archive file links the QR code
            - The original file is unchanged
            - The link is placed again after reprocessing
        """
        test_file = self.BARCODE_SAMPLE_DIR / "barcode-qr-url.pdf"
        dst = settings.SCRATCH_DIR / "barcode-qr-url.pdf"
        shutil.copy(test_file, dst)

        tasks.consume_file(
            ConsumableDocument(
                source=DocumentSource.ConsumeFolder,
                original_file=dst,
            ),
            None,
        )

        document = Document.objects.get()
        self.assertTrue(document.has_archive_version)
        links = self._link_annotations(document.archive_path, 1)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0][0], "https://example.com/invoice/4711")
        self.assertEqual(self._link_annotations(document.source_path, 1), [])
        self.assertEqual(document.source_path.read_bytes(), test_file.read_bytes())

        tasks.update_document_content_maybe_archive_file(document.pk)

        document.refresh_from_db()
        links = self._link_annotations(document.archive_path, 1)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0][0], "https://example.com/invoice/4711")

    @override_settings(
        CONSUMER_ENABLE_BARCODE_LINKS=True,
        CELERY_TASK_ALWAYS_EAGER=True,
        OCR_MODE="auto",
    )
    @pytest.mark.usefixtures("fake_progress_manager")
    def test_consume_file_link_failure_is_not_fatal(self) -> None:
        """
        GIVEN:
            - PDF with a URL QR code
            - Placing the links fails
        WHEN:
            - File is consumed
        THEN:
            - The document is still consumed, just without links
        """
        test_file = self.BARCODE_SAMPLE_DIR / "barcode-qr-url.pdf"
        dst = settings.SCRATCH_DIR / "barcode-qr-url.pdf"
        shutil.copy(test_file, dst)

        with mock.patch(
            "documents.consumer.locate_barcodes",
            side_effect=RuntimeError("broken"),
        ):
            tasks.consume_file(
                ConsumableDocument(
                    source=DocumentSource.ConsumeFolder,
                    original_file=dst,
                ),
                None,
            )

        document = Document.objects.get()
        self.assertTrue(document.has_archive_version)
        self.assertEqual(self._link_annotations(document.archive_path, 1), [])

    @override_settings(
        CONSUMER_ENABLE_BARCODE_LINKS=True,
        CONSUMER_STORE_BARCODE_VALUES=True,
        CELERY_TASK_ALWAYS_EAGER=True,
        OCR_MODE="auto",
    )
    @pytest.mark.usefixtures("fake_progress_manager")
    def test_consume_version_links_archive(self) -> None:
        """
        GIVEN:
            - A document with a URL QR code on page 2
            - Barcode links and storing barcode values enabled
        WHEN:
            - A rotated copy is consumed as a new version, like after
              rotating pages in the UI (versions skip the barcode plugin)
        THEN:
            - The archive file of the version links the QR code
            - The version stores the barcodes with their position in its own
              archive file
        """
        test_file = self.BARCODE_SAMPLE_DIR / "barcode-qr-url.pdf"
        dst = settings.SCRATCH_DIR / "barcode-qr-url.pdf"
        shutil.copy(test_file, dst)
        tasks.consume_file(
            ConsumableDocument(source=DocumentSource.ConsumeFolder, original_file=dst),
            None,
        )
        root = Document.objects.get()

        version_file = settings.SCRATCH_DIR / "barcode-qr-url-rotated.pdf"
        with Pdf.open(test_file) as pdf:
            for page in pdf.pages:
                page.rotate(90, relative=True)
            pdf.save(version_file)
        tasks.consume_file(
            ConsumableDocument(
                source=DocumentSource.ApiUpload,
                original_file=version_file,
                root_document_id=root.pk,
            ),
            None,
        )

        version = Document.objects.get(root_document=root)
        self.assertTrue(version.has_archive_version)
        links = self._link_annotations(version.archive_path, 1)
        self.assertEqual(
            [x[0] for x in links],
            ["https://example.com/invoice/4711"],
        )
        stored = version.barcodes.get(page=2)
        self.assertEqual(stored.value, "https://example.com/invoice/4711")
        # the stored position is the one of the link in the version's archive
        for stored_edge, link_edge in zip(stored.rect, links[0][1], strict=True):
            self.assertAlmostEqual(stored_edge, link_edge, delta=1)


class TestBarcodeValues(
    DirectoriesMixin,
    SampleDirMixin,
    GetReaderPluginMixin,
    TestCase,
):
    SAMPLE_VALUES = [
        {"page": 1, "value": "javascript:alert(1)", "format": "QR Code"},
        {"page": 2, "value": "https://example.com/invoice/4711", "format": "QR Code"},
    ]

    # Area of the pasted QR image on each page of barcode-qr-url.pdf, in PDF points
    QR_AREA = (403.2, 73.9, 547.2, 217.9)

    def _assert_rect_over_qr(self, rect: list[float]) -> None:
        self.assertIsNotNone(rect)
        x0, y0, x1, y1 = self.QR_AREA
        self.assertGreaterEqual(rect[0], x0)
        self.assertGreaterEqual(rect[1], y0)
        self.assertLessEqual(rect[2], x1)
        self.assertLessEqual(rect[3], y1)
        self.assertGreater(rect[2] - rect[0], 100)

    def test_locate_barcodes(self) -> None:
        """
        GIVEN:
            - PDF with a QR code on each of its two pages
        WHEN:
            - The barcodes are located, on all pages or on some
        THEN:
            - Each barcode is found with its page, value, format and position
        """
        test_file = self.BARCODE_SAMPLE_DIR / "barcode-qr-url.pdf"

        located = locate_barcodes(
            test_file,
            None,
            BarcodeConfig(),
            self.dirs.scratch_dir,
        )

        self.assertEqual(
            [(x.page, x.value, x.format) for x in located],
            [
                (0, "javascript:alert(1)", "QR Code"),
                (1, "https://example.com/invoice/4711", "QR Code"),
            ],
        )
        for barcode in located:
            self._assert_rect_over_qr(barcode.rect)

        located = locate_barcodes(
            test_file,
            [1, 7],
            BarcodeConfig(),
            self.dirs.scratch_dir,
        )
        self.assertEqual([x.page for x in located], [1])

    def test_attach_barcode_rects(self) -> None:
        """
        GIVEN:
            - Stored barcodes, including the same value twice on a page
            - Located barcodes, missing one of them
        WHEN:
            - Positions are attached
        THEN:
            - Each located barcode is used once, by page and value
            - A barcode not located again has no position
        """
        stored = [
            {"page": 1, "value": "A", "format": "QR Code"},
            {"page": 1, "value": "A", "format": "QR Code"},
            {"page": 2, "value": "A", "format": "QR Code"},
            {"page": 2, "value": "B", "format": "QR Code"},
        ]
        located = [
            LocatedBarcode(0, "A", "QR Code", (1.0, 2.0, 3.0, 4.0)),
            LocatedBarcode(0, "A", "QR Code", (5.0, 6.0, 7.0, 8.0)),
            LocatedBarcode(1, "B", "QR Code", (1.234, 2.0, 3.0, 4.0)),
        ]

        result = attach_barcode_rects(stored, located)

        self.assertEqual(
            [x["rect"] for x in result],
            [[1.0, 2.0, 3.0, 4.0], [5.0, 6.0, 7.0, 8.0], None, [1.23, 2.0, 3.0, 4.0]],
        )

    @override_settings(CONSUMER_STORE_BARCODE_VALUES=True)
    def test_values_detected(self) -> None:
        """
        GIVEN:
            - PDF with a QR code on each of its two pages
            - Storing barcode values enabled
        WHEN:
            - The barcode plugin runs
        THEN:
            - Both barcodes are remembered with page, value and format
        """
        test_file = self.BARCODE_SAMPLE_DIR / "barcode-qr-url.pdf"

        with self.get_reader(test_file) as reader:
            self.assertTrue(reader.able_to_run)
            reader.run()
            self.assertEqual(reader.metadata.barcodes, self.SAMPLE_VALUES)

    def test_values_disabled(self) -> None:
        """
        GIVEN:
            - PDF with QR codes
            - Storing barcode values not enabled
        WHEN:
            - The barcode plugin runs
        THEN:
            - No barcodes are remembered
        """
        test_file = self.BARCODE_SAMPLE_DIR / "barcode-qr-url.pdf"

        with self.get_reader(test_file) as reader:
            reader.run()
            self.assertIsNone(reader.metadata.barcodes)

    @override_settings(
        CONSUMER_STORE_BARCODE_VALUES=True,
        CELERY_TASK_ALWAYS_EAGER=True,
        OCR_MODE="auto",
    )
    @pytest.mark.usefixtures("fake_progress_manager")
    def test_consume_file_stores_values(self) -> None:
        """
        GIVEN:
            - PDF with a QR code on each of its two pages
            - Storing barcode values enabled
        WHEN:
            - File is consumed, the values are lost, and the document is reprocessed
        THEN:
            - The barcodes are stored with the document and shown in its metadata
            - Reprocessing reads them again
        """
        test_file = self.BARCODE_SAMPLE_DIR / "barcode-qr-url.pdf"
        dst = settings.SCRATCH_DIR / "barcode-qr-url.pdf"
        shutil.copy(test_file, dst)

        tasks.consume_file(
            ConsumableDocument(
                source=DocumentSource.ConsumeFolder,
                original_file=dst,
            ),
            None,
        )

        document = Document.objects.get()
        self.assertEqual(
            list(document.barcodes.values("page", "value", "format")),
            self.SAMPLE_VALUES,
        )

        user = User.objects.create_superuser(username="admin")
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(f"/api/documents/{document.pk}/metadata/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        barcodes = response.data["barcodes"]
        self.assertEqual(
            [{k: x[k] for k in ("page", "value", "format")} for x in barcodes],
            self.SAMPLE_VALUES,
        )
        # positions in the shown (archive) file, over the pasted QR images
        for barcode in barcodes:
            self._assert_rect_over_qr(barcode["rect"])

        # also part of the document itself, and searchable by content
        response = client.get(f"/api/documents/{document.pk}/")
        self.assertEqual(response.data["barcodes"], barcodes)
        response = client.get("/api/documents/?query=barcodes:invoice")
        self.assertEqual([x["id"] for x in response.data["results"]], [document.pk])
        response = client.get("/api/documents/?query=invoice")
        self.assertEqual(response.data["results"], [])

        document.barcodes.all().delete()
        tasks.update_document_content_maybe_archive_file(document.pk)

        self.assertEqual(
            list(document.barcodes.values("page", "value", "format")),
            self.SAMPLE_VALUES,
        )

    @override_settings(
        CELERY_TASK_ALWAYS_EAGER=True,
        OCR_MODE="auto",
    )
    @pytest.mark.usefixtures("fake_progress_manager")
    def test_consume_file_values_disabled(self) -> None:
        """
        GIVEN:
            - PDF with QR codes
            - Storing barcode values not enabled
        WHEN:
            - File is consumed
        THEN:
            - Nothing is stored and the metadata lists no barcodes
        """
        test_file = self.BARCODE_SAMPLE_DIR / "barcode-qr-url.pdf"
        dst = settings.SCRATCH_DIR / "barcode-qr-url.pdf"
        shutil.copy(test_file, dst)

        tasks.consume_file(
            ConsumableDocument(
                source=DocumentSource.ConsumeFolder,
                original_file=dst,
            ),
            None,
        )

        document = Document.objects.get()
        self.assertFalse(document.barcodes.exists())

    @override_settings(
        CONSUMER_STORE_BARCODE_VALUES=True,
        CELERY_TASK_ALWAYS_EAGER=True,
        OCR_MODE="auto",
    )
    @pytest.mark.usefixtures("fake_progress_manager")
    def test_consume_version_stores_own_values(self) -> None:
        """
        GIVEN:
            - A document with stored barcodes
            - Storing barcode values enabled
        WHEN:
            - A new version with a different barcode is consumed, like after
              rotating or removing pages
        THEN:
            - The version keeps its own barcodes, the original ones are kept
            - The document API and the search use those of the newest version
        """
        test_file = self.BARCODE_SAMPLE_DIR / "barcode-qr-url.pdf"
        dst = settings.SCRATCH_DIR / "barcode-qr-url.pdf"
        shutil.copy(test_file, dst)
        tasks.consume_file(
            ConsumableDocument(source=DocumentSource.ConsumeFolder, original_file=dst),
            None,
        )
        root = Document.objects.get()

        version_file = settings.SCRATCH_DIR / "barcode-128-custom.pdf"
        shutil.copy(self.BARCODE_SAMPLE_DIR / "barcode-128-custom.pdf", version_file)
        tasks.consume_file(
            ConsumableDocument(
                source=DocumentSource.ApiUpload,
                original_file=version_file,
                root_document_id=root.pk,
            ),
            None,
        )

        version = Document.objects.get(root_document=root)
        self.assertEqual(
            list(version.barcodes.values("page", "value", "format")),
            [{"page": 1, "value": "CUSTOM BARCODE", "format": "Code 128"}],
        )
        self.assertEqual(root.barcodes.count(), 2)
        self.assertEqual(
            [x.value for x in root.get_effective_barcodes()],
            ["CUSTOM BARCODE"],
        )

        user = User.objects.create_superuser(username="admin")
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(f"/api/documents/{root.pk}/")
        self.assertEqual(
            [x["value"] for x in response.data["barcodes"]],
            ["CUSTOM BARCODE"],
        )
        response = client.get('/api/documents/?query=barcodes:"custom barcode"')
        self.assertEqual([x["id"] for x in response.data["results"]], [root.pk])
        response = client.get("/api/documents/?query=barcodes:invoice")
        self.assertEqual(response.data["results"], [])
