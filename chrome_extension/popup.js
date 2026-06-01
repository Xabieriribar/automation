document.addEventListener("DOMContentLoaded", () => {
  const generateBtn = document.getElementById("generate-btn");
  const copyBtn = document.getElementById("copy-btn");
  const pdfBtn = document.getElementById("pdf-btn");
  const shareBtn = document.getElementById("share-btn");
  const plateInput = document.getElementById("plate");
  const marginSelect = document.getElementById("margin");
  const loader = document.getElementById("loader");
  const resultsContainer = document.getElementById("results");
  const devisTextPre = document.getElementById("devis-text");

  // Production API Gateway (strictly compliant with Web Store security policies)
  const API_ENDPOINT = "https://automation-dsni.onrender.com/api/generate-devis";

  // In-memory document buffers
  let currentPdfBase64 = null;
  let currentPlate = "devis";

  // Load saved configurations from Chrome Storage
  chrome.storage.local.get(["licensePlate"], (data) => {
    if (data.licensePlate) {
      plateInput.value = data.licensePlate;
    }
  });

  generateBtn.addEventListener("click", async () => {
    const plate = plateInput.value.trim();
    const margin = parseFloat(marginSelect.value);

    if (!plate) {
      alert("Veuillez saisir la plaque d'immatriculation.");
      return;
    }

    // Save inputs in storage
    chrome.storage.local.set({ licensePlate: plate });

    // Reset UI states
    loader.style.display = "flex";
    resultsContainer.style.display = "none";
    generateBtn.disabled = true;
    currentPdfBase64 = null;
    currentPlate = plate;

    try {
      // 1. Get active browser tab
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      if (!tab) {
        throw new Error("Aucun onglet actif détecté.");
      }

      // 2. Strict system protocol guard to prevent runtime crashes on native browser pages
      if (tab.url.startsWith("chrome://") || tab.url.startsWith("about:") || tab.url.startsWith("moz-extension://")) {
        throw new Error("Impossible d'analyser cette page système. Veuillez naviguer sur un site de pièces.");
      }

      // 3. Execute content script to scrape text
      const results = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        files: ["content.js"]
      });

      const extractedText = results[0]?.result?.text;
      if (!extractedText) {
        throw new Error("Impossible d'extraire le texte de cet onglet. Assurez-vous d'être sur une page web de pièces.");
      }

      // 4. Send extraction payload to the production backend
      const response = await fetch(API_ENDPOINT, {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          webpage_text: extractedText,
          license_plate: plate,
          margin_percentage: margin
        })
      });

      // Catch bad gateway / gateway timeout (typical Render cold starts)
      if (response.status === 502 || response.status === 504) {
        throw new Error("Le serveur Render se réveille (Cold Start). Veuillez patienter 10 secondes et cliquer de nouveau sur 'Générer'.");
      }

      if (!response.ok) {
        throw new Error(`Le serveur a répondu avec une erreur : ${response.status}`);
      }

      const data = await response.json();

      if (data.error) {
        throw new Error(data.error);
      }

      // 5. Cache structured PDF response
      devisTextPre.textContent = data.devis;
      currentPdfBase64 = data.pdf_base64;
      currentPlate = data.plate || plate;
      
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
        copyBtn.textContent = "Copier";
      }, 2000);
    }).catch(err => {
      console.error("Failed to copy devis text: ", err);
    });
  });

  // Share generated devis via WhatsApp Web
  shareBtn.addEventListener("click", () => {
    const textToShare = devisTextPre.textContent;
    const whatsappUrl = `https://api.whatsapp.com/send?text=${encodeURIComponent(textToShare)}`;
    window.open(whatsappUrl, "_blank");
  });
});
