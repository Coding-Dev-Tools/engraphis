"""Immutable oracle for grove-violet:code_relationships."""
import service


def main():
    assert service.relationship_target() == 'guarded_delivery_helper_violet', service.relationship_target()


if __name__ == "__main__":
    main()
