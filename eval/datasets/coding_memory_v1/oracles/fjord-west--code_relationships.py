"""Immutable oracle for fjord-west:code_relationships."""
import service


def main():
    assert service.relationship_target() == 'guarded_settlement_helper_west', service.relationship_target()


if __name__ == "__main__":
    main()
