(() => {
  // Directly grab all visible text from the page DOM
  const pageText = document.body.innerText || "";
  
  // Return the scraped text payload to the popup context
  return {
    text: pageText
  };
})();
