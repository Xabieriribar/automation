document.addEventListener("DOMContentLoaded", () => {
  const generateBtn = document.getElementById("generate-btn");
  const copyBtn = document.getElementById("copy-btn");
  const shareBtn = document.getElementById("share-btn");
  const plateInput = document.getElementById("plate");
  const marginSelect = document.getElementById("margin");
  const backendUrlInput = document.getElementById("backend-url");
  const loader = document.getElementById("loader");
  const resultsContainer = document.getElementById("results");
  const devisTextPre = document.getElementById("devis-text");

  // Load saved configurations from Chrome Storage
  chrome.storage.local.get(["backendUrl", "licensePlate"], (data) => {
    if (data.backendUrl) {
      backendUrlInput.value = data.backendUrl;
    }
    if (data.licensePlate) {
      plateInput.value = data.licensePlate;
    }
  });

  generateBtn.addEventListener("click", async () => {
    const plate = plateInput.value.trim();
    const margin = parseFloat(marginSelect.value);
    const backendUrl = backendUrlInput.value.trim();

    if (!plate) {
      alert("Veuillez saisir la plaque d'immatriculation.");
      return;
    }

    // Save inputs in storage
    chrome.storage.local.set({ backendUrl, licensePlate: plate });

    // Reset UI states
    loader.style.display = "flex";
    resultsContainer.style.display = "none";
    generateBtn.disabled = true;

    try {
      // 1. Get active browser tab
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      if (!tab) {
        throw new Error("Aucun onglet actif détecté.");
      }

      // 2. Execute content script to scrape text
      const results = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        files: ["content.js"]
      });

      const extractedText = results[0]?.result?.text;
      if (!extractedText) {
        throw new Error("Impossible d'extraire le texte de cet onglet.");
      }

      // 3. Send extraction payload to the backend
      const response = await fetch(`${backendUrl}/api/generate-devis`, {
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

      const data = await response.json();

      if (data.error) {
        throw new Error(data.error);
      }

      // 4. Populate results area
      devisTextPre.textContent = data.devis;
      resultsContainer.style.display = "block";
    } catch (err) {
      alert(`Erreur de génération : ${err.message}`);
    } finally {
      loader.style.display = "none";
      generateBtn.disabled = false;
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
