
// All Bokeh Models and DataSources have an 'id' property.
export class ModelManager<T extends { id: string }> {
  private instances: Map<string, T> = new Map();

  register(instance: T): void {
    // Access the ID directly as 'instance.id'
    this.instances.set(instance.id, instance);
    console.log(`Registered instance of type ${instance.constructor.name} with id: ${instance.id}`);
  }

  unregister(instance: T): void {
    // Access the ID directly as 'instance.id'
    this.instances.delete(instance.id);
    console.log(`Unregistered instance of type ${instance.constructor.name} with id: ${instance.id}`);
  }

  getInstances(): T[] {
    return Array.from(this.instances.values());
  }

  getInstance(id: string): T | undefined {
    return this.instances.get(id);
  }
}
