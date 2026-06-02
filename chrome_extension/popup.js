document.addEventListener("DOMContentLoaded", () => {
  const generateBtn = document.getElementById("generate-btn");
  const copyBtn = document.getElementById("copy-btn");
  const copyCsvBtn = document.getElementById("copy-csv-btn");
  const csvBtn = document.getElementById("csv-btn");
  const pdfBtn = document.getElementById("pdf-btn");
  const shareBtn = document.getElementById("share-btn");
  const plateInput = document.getElementById("plate");
  const clientNameInput = document.getElementById("client-name");
  const vehicleLabelInput = document.getElementById("vehicle-label");
  const operationTypeInput = document.getElementById("operation-type");
  const laborHoursInput = document.getElementById("labor-hours");
  const hourlyRateInput = document.getElementById("hourly-rate");
  const feeLabelInput = document.getElementById("fee-label");
  const feeAmountInput = document.getElementById("fee-amount");
  const marginSelect = document.getElementById("margin");
  const exportTargetSelect = document.getElementById("export-target");
  const loader = document.getElementById("loader");
  const resultsContainer = document.getElementById("results");
  const devisTextPre = document.getElementById("devis-text");
  const partsSummaryList = document.getElementById("parts-summary");
  const totalsSummary = document.getElementById("totals-summary");
  const warningsBox = document.getElementById("warnings");

  // Production API Gateway (strictly compliant with Web Store security policies)
  const API_ENDPOINT = "https://automation-dsni.onrender.com/api/generate-devis";

  // In-memory document buffers
  let currentPdfBase64 = null;
  let currentCsvText = "";
  let currentPlate = "devis";

  // Load saved configurations from Chrome Storage
  chrome.storage.local.get([
    "licensePlate",
    "clientName",
    "vehicleLabel",
    "operationType",
    "laborHours",
    "hourlyRate",
    "feeLabel",
    "feeAmount",
    "marginPercentage",
    "exportTarget"
  ], (data) => {
    if (data.licensePlate) {
      plateInput.value = data.licensePlate;
    }
    if (data.clientName) {
      clientNameInput.value = data.clientName;
    }
    if (data.vehicleLabel) {
      vehicleLabelInput.value = data.vehicleLabel;
    }
    if (data.operationType) {
      operationTypeInput.value = data.operationType;
    }
    if (data.laborHours) {
      laborHoursInput.value = data.laborHours;
    }
    if (data.hourlyRate) {
      hourlyRateInput.value = data.hourlyRate;
    }
    if (data.feeLabel) {
      feeLabelInput.value = data.feeLabel;
    }
    if (data.feeAmount) {
      feeAmountInput.value = data.feeAmount;
    }
    if (data.marginPercentage) {
      marginSelect.value = data.marginPercentage;
    }
    if (data.exportTarget) {
      exportTargetSelect.value = data.exportTarget;
    }
  });

  pdfBtn.disabled = true;
  csvBtn.disabled = true;
  copyCsvBtn.disabled = true;

  const clearNode = (node) => {
    while (node.firstChild) {
      node.removeChild(node.firstChild);
    }
  };

  const parseOptionalNumber = (input, label) => {
    const rawValue = input.value.trim();
    if (!rawValue) {
      return undefined;
    }
    const value = Number(rawValue);
    if (!Number.isFinite(value)) {
      throw new Error(`${label} doit être un nombre valide.`);
    }
    return value;
  };

  const formatChf = (value) => {
    const amount = Number(value || 0);
    return `CHF ${amount.toFixed(2)}`;
  };

  const isBlockedTabUrl = (url) => {
    if (!url) {
      return true;
    }
    return [
      "chrome://",
      "about:",
      "edge://",
      "devtools://",
      "chrome-extension://",
      "moz-extension://",
      "view-source:",
      "file://"
    ].some((prefix) => url.startsWith(prefix));
  };

  const extractServerError = (data, fallback) => {
    if (!data) {
      return fallback;
    }
    if (typeof data.error === "string") {
      return data.error;
    }
    if (typeof data.detail === "string") {
      return data.detail;
    }
    if (Array.isArray(data.detail)) {
      return data.detail.map((item) => item.msg || String(item)).join(", ");
    }
    return fallback;
  };

  const appendListItem = (list, text) => {
    const item = document.createElement("li");
    item.textContent = text;
    list.appendChild(item);
  };

  const renderLinesSummary = (parts, labor, fees) => {
    clearNode(partsSummaryList);
    const lines = [];

    if (Array.isArray(labor)) {
      labor.forEach((item) => {
        lines.push(`${item.hours || 0} h MO - ${item.description || "Main-d'œuvre"} - ${formatChf(item.total_ht)} HT`);
      });
    }

    if (Array.isArray(parts)) {
      parts.forEach((part) => {
        const reference = part.reference ? ` ${part.reference}` : "";
        const confidence = typeof part.confidence === "number" ? `, confiance ${Math.round(part.confidence * 100)}%` : "";
        lines.push(`${part.quantity || 1} x${reference} ${part.description || "Pièce"} - ${formatChf(part.total_ht)} HT${confidence}`);
      });
    }

    if (Array.isArray(fees)) {
      fees.forEach((fee) => {
        lines.push(`Frais - ${fee.description || "Frais annexes"} - ${formatChf(fee.amount_ht)} HT`);
      });
    }

    if (lines.length === 0) {
      appendListItem(partsSummaryList, "Aucune ligne générée. Vérifiez le panier ou ajoutez une ligne de main-d'œuvre/frais.");
      return;
    }

    lines.slice(0, 10).forEach((line) => appendListItem(partsSummaryList, line));

    if (lines.length > 10) {
      appendListItem(partsSummaryList, `+ ${lines.length - 10} autre(s) ligne(s) dans le devis.`);
    }
  };

  const addTotalRow = (label, value) => {
    const labelNode = document.createElement("span");
    labelNode.textContent = label;
    const amountNode = document.createElement("span");
    amountNode.className = "amount";
    amountNode.textContent = formatChf(value);
    totalsSummary.appendChild(labelNode);
    totalsSummary.appendChild(amountNode);
  };

  const renderTotalsSummary = (totals) => {
    clearNode(totalsSummary);
    addTotalRow("Main-d'œuvre HT", totals.total_labor_ht);
    addTotalRow("Pièces HT", totals.total_parts_ht);
    addTotalRow("Frais HT", totals.total_fees_ht);
    addTotalRow("Total HT", totals.total_ht);
    addTotalRow("TVA 8.1%", totals.tva_amount);
    addTotalRow("Total TTC", totals.total_ttc);
  };

  const renderWarnings = (warnings) => {
    clearNode(warningsBox);
    if (!Array.isArray(warnings) || warnings.length === 0) {
      warningsBox.style.display = "none";
      return;
    }

    warnings.forEach((warning) => {
      const warningLine = document.createElement("div");
      warningLine.textContent = warning;
      warningsBox.appendChild(warningLine);
    });
    warningsBox.style.display = "block";
  };

  generateBtn.addEventListener("click", async () => {
    const plate = plateInput.value.trim();
    const margin = parseFloat(marginSelect.value);
    const clientName = clientNameInput.value.trim();
    const vehicleLabel = vehicleLabelInput.value.trim();
    const operationType = operationTypeInput.value.trim();
    const feeLabel = feeLabelInput.value.trim();
    const exportTarget = exportTargetSelect.value;
    let laborHours;
    let hourlyRate;
    let feeAmount;

    if (!plate) {
      alert("Veuillez saisir la plaque d'immatriculation.");
      return;
    }

    try {
      laborHours = parseOptionalNumber(laborHoursInput, "Les heures de main-d'œuvre");
      hourlyRate = parseOptionalNumber(hourlyRateInput, "Le taux horaire");
      feeAmount = parseOptionalNumber(feeAmountInput, "Le montant des frais");
    } catch (err) {
      alert(err.message);
      return;
    }

    // Save inputs in storage
    chrome.storage.local.set({
      licensePlate: plate,
      clientName,
      vehicleLabel,
      operationType,
      laborHours: laborHoursInput.value.trim(),
      hourlyRate: hourlyRateInput.value.trim(),
      feeLabel,
      feeAmount: feeAmountInput.value.trim(),
      marginPercentage: marginSelect.value,
      exportTarget
    });

    // Reset UI states
    loader.style.display = "flex";
    resultsContainer.style.display = "none";
    generateBtn.disabled = true;
    pdfBtn.disabled = true;
    csvBtn.disabled = true;
    copyCsvBtn.disabled = true;
    currentPdfBase64 = null;
    currentCsvText = "";
    currentPlate = plate;

    try {
      // 1. Get active browser tab
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      if (!tab) {
        throw new Error("Aucun onglet actif détecté.");
      }

      // 2. Strict system protocol guard to prevent runtime crashes on native browser pages
      if (isBlockedTabUrl(tab.url)) {
        throw new Error("Impossible d'analyser cette page système. Ouvrez le panier fournisseur dans un onglet web standard.");
      }

      // 3. Execute content script to scrape text
      const results = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        files: ["content.js"]
      });

      const extractedText = results[0]?.result?.text;
      if (!extractedText || extractedText.trim().length < 20) {
        throw new Error("La page ne contient pas assez de texte exploitable. Assurez-vous d'être sur le panier fournisseur visible.");
      }

      // 4. Send extraction payload to the production backend
      const payload = {
        webpage_text: extractedText,
        license_plate: plate,
        margin_percentage: margin,
        client_name: clientName || undefined,
        vehicle_label: vehicleLabel || undefined,
        operation_type: operationType || undefined,
        labor_hours: laborHours,
        hourly_rate: hourlyRate,
        fee_label: feeLabel || undefined,
        fee_amount_ht: feeAmount,
        export_target: exportTarget,
        bexio_dry_run: true
      };

      const response = await fetch(API_ENDPOINT, {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify(payload)
      });

      // Catch bad gateway / gateway timeout (typical Render cold starts)
      if (response.status === 502 || response.status === 504) {
        throw new Error("Le serveur Render se réveille (Cold Start). Veuillez patienter 10 secondes et cliquer de nouveau sur 'Générer'.");
      }

      const data = await response.json().catch(() => null);

      if (!response.ok) {
        throw new Error(extractServerError(data, `Le serveur a répondu avec une erreur : ${response.status}`));
      }

      if (!data || data.error) {
        throw new Error(extractServerError(data, "Réponse serveur invalide."));
      }

      // 5. Cache structured PDF response
      devisTextPre.textContent = data.devis || "";
      currentPdfBase64 = data.pdf_base64 || null;
      currentCsvText = data.csv || "";
      currentPlate = data.plate || plate;
      pdfBtn.disabled = !currentPdfBase64;
      csvBtn.disabled = !currentCsvText;
      copyCsvBtn.disabled = !currentCsvText;
      renderLinesSummary(data.parts || [], data.labor || [], data.fees || []);
      renderTotalsSummary(data.totals || {});
      renderWarnings(data.warnings || []);

      resultsContainer.style.display = "block";
    } catch (err) {
      alert(`Erreur de génération : ${err.message}`);
    } finally {
      loader.style.display = "none";
      generateBtn.disabled = false;
    }
  });

  // Convert Base64 string to Binary Blob
  const base64ToBlob = (base64, type = "application/pdf") => {
    const binStr = atob(base64);
    const len = binStr.length;
    const arr = new Uint8Array(len);
    for (let i = 0; i < len; i++) {
      arr[i] = binStr.charCodeAt(i);
    }
    return new Blob([arr], { type });
  };

  const downloadTextFile = (content, filename, type = "text/plain;charset=utf-8") => {
    const blob = new Blob([content], { type });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  csvBtn.addEventListener("click", () => {
    if (!currentCsvText) {
      alert("Aucun CSV disponible pour le téléchargement.");
      return;
    }
    downloadTextFile(currentCsvText, `devis_${currentPlate.replace(/\s+/g, "_")}.csv`, "text/csv;charset=utf-8");
  });

  copyCsvBtn.addEventListener("click", () => {
    if (!currentCsvText) {
      alert("Aucun CSV disponible à copier.");
      return;
    }
    navigator.clipboard.writeText(currentCsvText).then(() => {
      copyCsvBtn.textContent = "CSV copié !";
      setTimeout(() => {
        copyCsvBtn.textContent = "Copier CSV";
      }, 2000);
    }).catch(err => {
      console.error("Failed to copy CSV: ", err);
    });
  });

  // Safe client-side local A4 PDF download triggered from popup UI without storage extensions
  pdfBtn.addEventListener("click", () => {
    if (!currentPdfBase64) {
      alert("Aucun fichier PDF disponible pour le téléchargement.");
      return;
    }

    try {
      const blob = base64ToBlob(currentPdfBase64, "application/pdf");
      const url = URL.createObjectURL(blob);
      
      const a = document.createElement("a");
      a.href = url;
      a.download = `devis_${currentPlate.replace(/\s+/g, "_")}.pdf`;
      document.body.appendChild(a);
      a.click();
      
      // Clean up DOM and memory URL references immediately
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (err) {
      alert(`Erreur lors du téléchargement du PDF : ${err.message}`);
    }
  });

  // Copy devis text to Clipboard
  copyBtn.addEventListener("click", () => {
    const textToCopy = devisTextPre.textContent;
    navigator.clipboard.writeText(textToCopy).then(() => {
      copyBtn.textContent = "Copié !";
      setTimeout(() => {
        copyBtn.textContent = "Copier texte";
      }, 2000);
    }).catch(err => {
      console.error("Failed to copy devis text: ", err);
    });
  });

  // Share generated devis via WhatsApp Web
  shareBtn.addEventListener("click", () => {
    const textToShare = devisTextPre.textContent;
    const whatsappUrl = `https://api.whatsapp.com/send?text=${encodeURIComponent(textToShare)}`;
    window.open(whatsappUrl, "_blank", "noopener,noreferrer");
  });
});
