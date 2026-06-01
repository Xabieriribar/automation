(() => {
  // Directly grab all visible text from the page DOM with optional chaining to prevent crashes
  const pageText = document.body?.innerText || "";
  
  // Return the scraped text payload to the popup context
  return {
    text: pageText
  };
})();
