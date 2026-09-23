"""Immutable oracle for borealis-green:code_relationships."""
import service


def main():
    assert service.relationship_target() == 'guarded_ledger_helper_green', service.relationship_target()


if __name__ == "__main__":
    main()
