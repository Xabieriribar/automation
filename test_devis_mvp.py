import asyncio
import os

os.environ["GEMINI_API_KEY"] = ""

from main import (  # noqa: E402
    DevisRequest,
    api_generate_devis,
    build_devis_data,
    build_invoice_csv,
    create_structured_devis_pdf,
    extract_parts_with_fallback,
)


SAMPLE_CART_TEXT = """
Derendinger panier
Plaquettes de frein avant
Reference 0986424738
Marque Bosch
Quantite 2
Prix unitaire CHF 50.00
Sous-total CHF 100.00
"""


def dump_model(model):
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


def test_parsing_fallback_extracts_visible_cart_line():
    parts, warnings = extract_parts_with_fallback(SAMPLE_CART_TEXT, "Derendinger")

    assert not warnings
    assert len(parts) == 1
    assert parts[0].reference == "0986424738"
    assert parts[0].description == "Plaquettes de frein avant"
    assert parts[0].quantity == 2
    assert parts[0].unit_price_ht == 50.0
    assert parts[0].total_ht == 100.0


def test_margin_applies_only_to_parts():
    request = DevisRequest(
        webpage_text=SAMPLE_CART_TEXT,
        license_plate="VD 123456",
        margin_percentage=20,
        operation_type="Freins avant",
        labor_hours=2,
        hourly_rate=100,
    )
    data = build_devis_data(request)

    assert len(data.parts) == 1
    assert data.parts[0].unit_price_ht == 60.0
    assert data.parts[0].total_ht == 120.0
    assert data.labor[0].total_ht == 200.0
    assert data.totals["total_parts_ht"] == 120.0
    assert data.totals["total_labor_ht"] == 200.0
    assert data.totals["total_fees_ht"] == 0.0


def test_vat_totals_with_optional_fee():
    request = DevisRequest(
        webpage_text=SAMPLE_CART_TEXT,
        license_plate="VD 123456",
        margin_percentage=0,
        fee_label="Recyclage",
        fee_amount_ht=15,
    )
    data = build_devis_data(request)

    assert data.labor == []
    assert data.fees[0].amount_ht == 15.0
    assert data.totals["total_ht"] == 115.0
    assert data.totals["tva_rate"] == 0.081
    assert data.totals["tva_amount"] == 9.32
    assert data.totals["total_ttc"] == 124.32


def test_csv_generation():
    request = DevisRequest(
        webpage_text=SAMPLE_CART_TEXT,
        license_plate="VD 123456",
        margin_percentage=10,
    )
    data = build_devis_data(request)
    csv_text = build_invoice_csv(data)

    assert "position;type;reference;description;quantity;unit;unit_price_ht;total_ht;vat_rate" in csv_text
    assert "part;0986424738;Plaquettes de frein avant;2;pc;55.00;110.00;8.1" in csv_text


def test_pdf_smoke():
    request = DevisRequest(
        webpage_text=SAMPLE_CART_TEXT,
        license_plate="VD 123456",
        margin_percentage=0,
    )
    data = build_devis_data(request)
    pdf_bytes = create_structured_devis_pdf(data)

    assert pdf_bytes.startswith(b"%PDF")
    assert len(pdf_bytes) > 1000


def test_endpoint_response_shape():
    request = DevisRequest(
        webpage_text=SAMPLE_CART_TEXT,
        license_plate="VD 123456",
        margin_percentage=10,
        labor_hours=1,
        hourly_rate=120,
    )
    response = asyncio.run(api_generate_devis(request))
    payload = dump_model(response)

    for key in ["devis", "csv", "pdf_base64", "plate", "parts", "labor", "fees", "totals", "warnings", "exports"]:
        assert key in payload
    assert payload["plate"] == "VD 123456"
    assert payload["parts"]
    assert payload["totals"]["total_ttc"] > 0
    assert payload["exports"]["csv"]["available"] is True


if __name__ == "__main__":
    test_parsing_fallback_extracts_visible_cart_line()
    test_margin_applies_only_to_parts()
    test_vat_totals_with_optional_fee()
    test_csv_generation()
    test_pdf_smoke()
    test_endpoint_response_shape()
    print("All devis MVP tests passed.")
