"""Immutable oracle for delta-green:code_relationships."""
import service


def main():
    assert service.relationship_target() == 'guarded_index_helper_green', service.relationship_target()


if __name__ == "__main__":
    main()
