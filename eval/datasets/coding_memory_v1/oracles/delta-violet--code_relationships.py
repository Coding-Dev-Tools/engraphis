"""Immutable oracle for delta-violet:code_relationships."""
import service


def main():
    assert service.relationship_target() == 'guarded_index_helper_violet', service.relationship_target()


if __name__ == "__main__":
    main()
