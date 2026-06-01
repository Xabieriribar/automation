// Lightweight content script to extract body inner text and send it to popup
(function() {
  const pageText = document.body ? document.body.innerText : "";
  return { text: pageText };
})();
