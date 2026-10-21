(function () {
  document.addEventListener("submit", (event) => {
    const form = event.target.closest("[data-confirm]");
    if (form && !window.confirm(form.dataset.confirm)) event.preventDefault();
  });

  function syncQuantityStepper(stepper) {
    const input = stepper.querySelector('input[name="quantity"]');
    if (!input) return;
    const value = input.valueAsNumber;
    const decreaseButton = stepper.querySelector('[data-quantity-step="-1"]');
    const increaseButton = stepper.querySelector('[data-quantity-step="1"]');
    if (decreaseButton) {
      decreaseButton.disabled = !Number.isFinite(value) || value <= Number(input.min);
    }
    if (increaseButton) {
      increaseButton.disabled = Number.isFinite(value) && value >= Number(input.max);
    }
  }

  document.querySelectorAll("[data-quantity-stepper]").forEach(syncQuantityStepper);
  document.addEventListener("click", (event) => {
    if (!(event.target instanceof Element)) return;
    const button = event.target.closest("[data-quantity-step]");
    const stepper = button?.closest("[data-quantity-stepper]");
    const input = stepper?.querySelector('input[name="quantity"]');
    if (!button || !stepper || !input) return;

    const min = Number(input.min);
    const max = Number(input.max);
    const current = input.valueAsNumber;
    const value = Number.isFinite(current) ? current : min;
    const nextValue = value + Number(button.dataset.quantityStep);
    input.value = String(Math.min(max, Math.max(min, nextValue)));
    syncQuantityStepper(stepper);
  });
  document.addEventListener("input", (event) => {
    if (!(event.target instanceof Element)) return;
    const stepper = event.target.closest("[data-quantity-stepper]");
    if (stepper) syncQuantityStepper(stepper);
  });

  const search = document.getElementById("product-search");
  const results = document.getElementById("product-results");
  const cartElement = document.getElementById("cart-items");
  const totalElement = document.getElementById("cart-total");
  const tenderedInput = document.getElementById("amount-tendered");
  const changeElement = document.getElementById("change-due");
  const checkoutButton = document.getElementById("checkout-button");
  const imageInput = document.querySelector("[data-image-input]");
  const chooseImageButton = document.querySelector("[data-choose-image]");
  const removeImageButton = document.querySelector("[data-remove-image]");
  const imagePreview = document.querySelector("[data-image-preview]");
  const imagePlaceholder = document.querySelector("[data-image-placeholder]");
  const imageName = document.querySelector("[data-image-name]");
  const imageError = document.querySelector("[data-image-error]");

  if (
    imageInput
    && chooseImageButton
    && removeImageButton
    && imagePreview
    && imagePlaceholder
    && imageName
    && imageError
  ) {
    let previewUrl;
    const clearImage = () => {
      imageInput.value = "";
      if (previewUrl) URL.revokeObjectURL(previewUrl);
      previewUrl = undefined;
      imagePreview.removeAttribute("src");
      imagePreview.classList.add("hidden");
      imagePlaceholder.classList.remove("hidden");
      removeImageButton.classList.add("hidden");
      imageName.textContent = "No photo selected";
    };

    chooseImageButton.addEventListener("click", () => imageInput.click());
    removeImageButton.addEventListener("click", () => {
      clearImage();
      imageError.textContent = "";
      imageError.classList.add("hidden");
    });
    imageInput.addEventListener("change", () => {
      const file = imageInput.files[0];
      imageError.textContent = "";
      imageError.classList.add("hidden");
      if (!file) {
        clearImage();
        return;
      }

      const supportedTypes = ["image/jpeg", "image/png", "image/webp"];
      const supportedExtension = /\.(jpe?g|png|webp)$/i.test(file.name);
      const supportedType = supportedTypes.includes(file.type.toLowerCase())
        || (!file.type && supportedExtension);
      if (!supportedType || !supportedExtension || file.size > 5 * 1024 * 1024) {
        clearImage();
        imageError.textContent = file.size > 5 * 1024 * 1024
          ? "The photo must be 5 MB or smaller."
          : "Choose a JPG, JPEG, PNG, or WebP image.";
        imageError.classList.remove("hidden");
        return;
      }

      if (previewUrl) URL.revokeObjectURL(previewUrl);
      previewUrl = URL.createObjectURL(file);
      imagePreview.src = previewUrl;
      imagePreview.classList.remove("hidden");
      imagePlaceholder.classList.add("hidden");
      imageName.textContent = file.name;
      removeImageButton.classList.remove("hidden");
    });
  }

  if (
    !search
    || !results
    || !cartElement
    || !tenderedInput
    || !changeElement
    || !checkoutButton
  ) return;

  const products = window.retailFlowProductData || [];
  const cart = new Map();
  const money = (value) => `₱${value.toFixed(2)}`;
  let previousTotal = 0;
  let saleSubmissionKey = null;

  function renderResults() {
    const query = search.value.trim().toLowerCase();
    if (!query) {
      results.classList.add("hidden");
      results.innerHTML = "";
      return;
    }
    const matches = products.filter((product) => {
      const searchableText = [
        product.name,
        product.sku,
        product.barcode,
        product.size || "",
        product.color || ""
      ].join(" ").toLowerCase();
      return searchableText.includes(query);
    });
    results.innerHTML = matches.length
      ? matches.map((product) => {
        const variant = [product.size, product.color]
          .filter(Boolean)
          .join(" / ");
        const productImage = product.image
          ? `<img class="product-photo" src="${escapeHtml(product.image)}" alt="">`
          : `<span class="product-mark">${
            escapeHtml(product.name.charAt(0).toUpperCase())
          }</span>`;
        return `<button type="button" class="product-result"
          data-product="${product.id}">
          ${productImage}
          <span class="row-grow">
            <strong>${escapeHtml(product.name)}${
              variant ? ` · ${escapeHtml(variant)}` : ""
            }</strong>
            <small>${escapeHtml(product.sku)} · ${product.stock} in stock</small>
          </span>
          <strong>${money(product.price)}</strong>
        </button>`;
      }).join("")
      : '<div class="result-empty">No matching products in stock.</div>';
    results.classList.remove("hidden");
  }

  function escapeHtml(value) {
    const htmlEntities = {
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;"
    };
    return String(value).replace(
      /[&<>"']/g,
      (character) => htmlEntities[character]
    );
  }

  function renderCart() {
    saleSubmissionKey = null;
    const items = [...cart.values()];
    const total = items.reduce((sum, item) => sum + item.price * item.quantity, 0);
    const tenderedBeforeUpdate = Number(tenderedInput.value);
    if (
      !tenderedInput.value
      || Math.abs(tenderedBeforeUpdate - previousTotal) < 0.005
    ) {
      tenderedInput.value = total.toFixed(2);
    }
    previousTotal = total;
    const tendered = Number(tenderedInput.value);
    const canPay = Number.isFinite(tendered) && tendered >= total;
    changeElement.textContent = money(Math.max(0, tendered - total));
    changeElement.classList.toggle("insufficient", !canPay && items.length > 0);
    totalElement.textContent = money(total);
    checkoutButton.disabled = items.length === 0 || !canPay;
    if (!items.length) {
      cartElement.innerHTML =
        '<div class="empty-state">Your cart is empty. '
        + 'Search for a product to get started.</div>';
      return;
    }
    cartElement.innerHTML = items.map((item) => {
      const productImage = item.image
        ? `<img class="product-photo" src="${escapeHtml(item.image)}" alt="">`
        : "";
      return `<div class="cart-row">
        ${productImage}
        <div class="row-grow">
          <strong>${escapeHtml(item.name)}</strong>
          <small>${money(item.price)} each</small>
        </div>
        <div class="quantity-control">
          <button type="button" data-action="decrease"
            data-product="${item.id}" aria-label="Decrease quantity">−</button>
          <span>${item.quantity}</span>
          <button type="button" data-action="increase"
            data-product="${item.id}" aria-label="Increase quantity">+</button>
        </div>
        <strong class="cart-line-total">
          ${money(item.price * item.quantity)}
        </strong>
      </div>`;
    }).join("");
  }

  results.addEventListener("click", (event) => {
    const button = event.target.closest("[data-product]");
    if (!button) return;
    const product = products.find((item) => item.id === Number(button.dataset.product));
    if (!product) return;
    const existing = cart.get(product.id);
    if ((existing?.quantity || 0) >= product.stock) {
      window.alert(`Only ${product.stock} ${product.name} available.`);
      return;
    }
    cart.set(product.id, { ...product, quantity: (existing?.quantity || 0) + 1 });
    search.value = "";
    results.classList.add("hidden");
    renderCart();
    search.focus();
  });

  cartElement.addEventListener("click", (event) => {
    const button = event.target.closest("[data-action]");
    if (!button) return;
    const id = Number(button.dataset.product);
    const item = cart.get(id);
    if (!item) return;
    if (button.dataset.action === "decrease" && item.quantity === 1) {
      cart.delete(id);
    }
    else if (button.dataset.action === "decrease") item.quantity -= 1;
    else if (item.quantity < item.stock) item.quantity += 1;
    else window.alert(`Only ${item.stock} ${item.name} available.`);
    renderCart();
  });

  search.addEventListener("input", renderResults);
  tenderedInput.addEventListener("input", renderCart);
  search.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      const first = results.querySelector("[data-product]");
      if (first) first.click();
    }
    if (event.key === "Escape") results.classList.add("hidden");
  });
  document.addEventListener("click", (event) => {
    if (!event.target.closest(".pos-search")) results.classList.add("hidden");
  });

  checkoutButton.addEventListener("click", async () => {
    if (!cart.size) return;
    if (!saleSubmissionKey) {
      saleSubmissionKey = window.crypto?.randomUUID
        ? window.crypto.randomUUID()
        : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    }
    checkoutButton.disabled = true;
    checkoutButton.textContent = "Processing…";
    try {
      const response = await fetch("/sales/", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-TOKEN": document.querySelector('meta[name="csrf-token"]')?.content || "",
          "Idempotency-Key": saleSubmissionKey
        },
        body: JSON.stringify({
          items: [...cart.values()].map(({ id, quantity }) => ({
            product_id: id,
            quantity
          })),
          amount_tendered: tenderedInput.value
        })
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Could not complete the sale.");
      window.location.href = result.invoice_url;
    } catch (error) {
      window.alert(error.message);
      checkoutButton.disabled = false;
      checkoutButton.innerHTML = 'Complete sale <span>→</span>';
    }
  });
})();
