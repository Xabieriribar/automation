#!/bin/bash

# Exit immediately if a command exits with a non-zero status
set -e

echo "=== Démarrage de la compilation de l'extension ==="

# 1. Nettoyer les anciens fichiers de build
echo "Nettoyage des anciens fichiers..."
rm -rf dist/
rm -f garage_devis_store.zip

# 2. Créer un répertoire de production temporaire
echo "Création du dossier de build dist/..."
mkdir -p dist

# 3. Copier les 4 fichiers cœurs requis de l'extension et le dossier d'icônes
echo "Copie des fichiers de l'extension..."
cp chrome_extension/manifest.json dist/
cp chrome_extension/popup.html dist/
cp chrome_extension/popup.js dist/
cp chrome_extension/content.js dist/
cp -r chrome_extension/icons dist/

# 4. Compresser les fichiers dans une archive zip de production
echo "Création de l'archive ZIP garage_devis_store.zip..."
cd dist
zip -r ../garage_devis_store.zip .
cd ..

# 5. Nettoyer le dossier temporaire
echo "Nettoyage temporaire du dossier dist/..."
rm -rf dist/

echo "=== Compilation terminée avec succès ! ==="
echo "Livrable généré : garage_devis_store.zip"
