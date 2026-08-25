export class CartService {
  async addItem(productId: string): Promise<string> {
    return `cart:${productId}`;
  }
}

const cartService = new CartService();

export async function checkoutRoute(productId: string): Promise<string> {
  return cartService.addItem(productId);
}

export async function publicCheckoutApi(productId: string): Promise<string> {
  return checkoutRoute(productId);
}
